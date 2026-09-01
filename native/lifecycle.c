#include "lifecycle.h"

#include <CommonCrypto/CommonDigest.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define CPL_FORMAT_VERSION 1U
#define CPL_HEADER_SIZE 108U
#define CPL_MAGIC_SIZE 8U
#define CPL_MAX_NAME 180U
#define CPL_LOCK_RETRY_NS 500000ULL
#define CPL_DEFAULT_DEADLINE_NS 1000000000ULL

static const uint8_t CPL_MAGIC[CPL_MAGIC_SIZE] = {
    'C', 'P', 'L', 'J', 'R', 'N', '0', '1'
};

struct cpl_journal {
    int fd;
    int append_lock_fd;
    int action_lock_fd;
    pthread_mutex_t append_mutex;
    pthread_mutex_t action_mutex;
    pid_t owner_pid;
    dev_t parent_dev;
    ino_t parent_ino;
    dev_t journal_dev;
    ino_t journal_ino;
    uint64_t normal_limit;
    uint64_t hard_limit;
    uint8_t nonce[CPL_HASH_SIZE];
    char journal_name[CPL_MAX_NAME + 1U];
    bool unhealthy;
};

_Static_assert(sizeof(struct cpl_record) == 456U,
    "cpl_record ABI layout changed");
_Static_assert(sizeof(struct cpl_state) == 432U,
    "cpl_state ABI layout changed");
_Static_assert(sizeof(struct cpl_chain) == 512U,
    "cpl_chain ABI layout changed");
_Static_assert(sizeof(struct cpl_certified_head) == 488U,
    "cpl_certified_head ABI layout changed");
_Static_assert(sizeof(struct cpl_create_receipt) == 36U,
    "cpl_create_receipt ABI layout changed");
_Static_assert(sizeof(struct cpl_delete_authority) == 68U,
    "cpl_delete_authority ABI layout changed");
_Static_assert(sizeof(struct cpl_delete_receipt) == 8U,
    "cpl_delete_receipt ABI layout changed");

static int validate_parent_identity(cpl_journal *journal, int parent_dirfd);

static uint64_t monotonic_ns(void) {
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC, &now) < 0) {
        return 0U;
    }
    return (uint64_t)now.tv_sec * 1000000000ULL + (uint64_t)now.tv_nsec;
}

static uint64_t effective_deadline(uint64_t deadline_ns) {
    uint64_t now = monotonic_ns();

    if (deadline_ns != 0U) {
        return deadline_ns;
    }
    if (UINT64_MAX - now < CPL_DEFAULT_DEADLINE_NS) {
        return UINT64_MAX;
    }
    return now + CPL_DEFAULT_DEADLINE_NS;
}

static void retry_pause(void) {
    const struct timespec delay = {0, (long)CPL_LOCK_RETRY_NS};

    (void)nanosleep(&delay, NULL);
}

static int take_mutex(pthread_mutex_t *mutex, uint64_t deadline_ns) {
    int status;

    for (;;) {
        status = pthread_mutex_trylock(mutex);
        if (status == 0) {
            return CPL_OK;
        }
        if (status != EBUSY) {
            return CPL_ERR_SYSTEM;
        }
        if (monotonic_ns() >= deadline_ns) {
            return CPL_ERR_LOCK_TIMEOUT;
        }
        retry_pause();
    }
}

static int take_flock(int fd, uint64_t deadline_ns) {
    for (;;) {
        if (flock(fd, LOCK_EX | LOCK_NB) == 0) {
            return CPL_OK;
        }
        if (errno != EWOULDBLOCK && errno != EAGAIN && errno != EINTR) {
            return CPL_ERR_SYSTEM;
        }
        if (monotonic_ns() >= deadline_ns) {
            return CPL_ERR_LOCK_TIMEOUT;
        }
        retry_pause();
    }
}

static int release_flock(int fd) {
    if (flock(fd, LOCK_UN) < 0) {
        return CPL_ERR_SYSTEM;
    }
    return CPL_OK;
}

static bool is_zero(const uint8_t *value, size_t length) {
    size_t index;

    for (index = 0U; index < length; ++index) {
        if (value[index] != 0U) {
            return false;
        }
    }
    return true;
}

static bool same_id(const uint8_t left[CPL_ID_SIZE],
    const uint8_t right[CPL_ID_SIZE]) {
    return memcmp(left, right, CPL_ID_SIZE) == 0;
}

static bool has_id(const uint8_t value[CPL_ID_SIZE]) {
    return !is_zero(value, CPL_ID_SIZE);
}

static void put_u16(uint8_t *target, uint16_t value) {
    target[0] = (uint8_t)(value & 0xffU);
    target[1] = (uint8_t)((value >> 8U) & 0xffU);
}

static void put_u32(uint8_t *target, uint32_t value) {
    target[0] = (uint8_t)(value & 0xffU);
    target[1] = (uint8_t)((value >> 8U) & 0xffU);
    target[2] = (uint8_t)((value >> 16U) & 0xffU);
    target[3] = (uint8_t)((value >> 24U) & 0xffU);
}

static void put_u64(uint8_t *target, uint64_t value) {
    size_t index;

    for (index = 0U; index < 8U; ++index) {
        target[index] = (uint8_t)((value >> (index * 8U)) & 0xffU);
    }
}

static uint16_t get_u16(const uint8_t *source) {
    return (uint16_t)((uint16_t)source[0] | ((uint16_t)source[1] << 8U));
}

static uint32_t get_u32(const uint8_t *source) {
    return (uint32_t)source[0] | ((uint32_t)source[1] << 8U) |
        ((uint32_t)source[2] << 16U) | ((uint32_t)source[3] << 24U);
}

static uint64_t get_u64(const uint8_t *source) {
    uint64_t value = 0U;
    size_t index;

    for (index = 0U; index < 8U; ++index) {
        value |= (uint64_t)source[index] << (index * 8U);
    }
    return value;
}

static uint32_t crc32c(const uint8_t *data, size_t length) {
    uint32_t crc = UINT32_MAX;
    size_t index;
    unsigned bit;

    for (index = 0U; index < length; ++index) {
        crc ^= data[index];
        for (bit = 0U; bit < 8U; ++bit) {
            uint32_t mask = (uint32_t)-(int32_t)(crc & 1U);

            crc = (crc >> 1U) ^ (0x82f63b78U & mask);
        }
    }
    return ~crc;
}

static void sha256(const uint8_t *data, size_t length,
    uint8_t out[CPL_HASH_SIZE]) {
    (void)CC_SHA256(data, (CC_LONG)length, out);
}

static int validate_component(const char *name) {
    size_t length;

    if (name == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    length = strnlen(name, CPL_MAX_NAME + 1U);
    if (length == 0U || length > CPL_MAX_NAME || strchr(name, '/') != NULL ||
        strcmp(name, ".") == 0 || strcmp(name, "..") == 0) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    return CPL_OK;
}

static int validate_parent(int parent_dirfd, struct stat *out) {
    if (parent_dirfd < 0 || out == NULL || fstat(parent_dirfd, out) < 0) {
        return CPL_ERR_SYSTEM;
    }
    if (!S_ISDIR(out->st_mode) || out->st_uid != geteuid() ||
        (out->st_mode & (mode_t)0777) != (mode_t)0700) {
        return CPL_ERR_UNSAFE_FILE;
    }
    return CPL_OK;
}

static int validate_regular_fd(int fd, dev_t required_dev, struct stat *out) {
    if (fstat(fd, out) < 0) {
        return CPL_ERR_SYSTEM;
    }
    if (!S_ISREG(out->st_mode) || out->st_uid != geteuid() ||
        (out->st_mode & (mode_t)0777) != (mode_t)0600 ||
        out->st_dev != required_dev) {
        return CPL_ERR_UNSAFE_FILE;
    }
    return CPL_OK;
}

static int validate_path_pair(int parent_dirfd, const char *name,
    const struct stat *opened) {
    struct stat path_stat;

    if (fstatat(parent_dirfd, name, &path_stat, AT_SYMLINK_NOFOLLOW) < 0) {
        if (errno == ENOENT) {
            return CPL_ERR_NOT_FOUND;
        }
        return CPL_ERR_SYSTEM;
    }
    if (S_ISLNK(path_stat.st_mode)) {
        return CPL_ERR_SYMLINK;
    }
    if (path_stat.st_dev != opened->st_dev || path_stat.st_ino != opened->st_ino) {
        return CPL_ERR_IDENTITY_DRIFT;
    }
    return CPL_OK;
}

static int open_lock_at(int parent_dirfd, const char *journal_name,
    const char *suffix, dev_t required_dev, int *out_fd) {
    char name[CPL_MAX_NAME + 32U];
    struct stat lock_stat;
    int written;
    int fd;
    int status;

    written = snprintf(name, sizeof(name), "%s%s", journal_name, suffix);
    if (written < 0 || (size_t)written >= sizeof(name)) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    fd = openat(parent_dirfd, name, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) {
        if (errno == ELOOP) {
            return CPL_ERR_SYMLINK;
        }
        if (errno == ENOENT) {
            return CPL_ERR_NOT_FOUND;
        }
        return CPL_ERR_SYSTEM;
    }
    status = validate_regular_fd(fd, required_dev, &lock_stat);
    if (status == CPL_OK) {
        status = validate_path_pair(parent_dirfd, name, &lock_stat);
    }
    if (status != CPL_OK) {
        (void)close(fd);
        return status;
    }
    *out_fd = fd;
    return CPL_OK;
}

static int initialize_handle(int fd, int parent_dirfd, const char *journal_name,
    const uint8_t nonce[CPL_HASH_SIZE], uint64_t normal_limit,
    uint64_t hard_limit, cpl_journal **out) {
    struct stat parent_stat;
    struct stat journal_stat;
    cpl_journal *journal;
    int append_fd = -1;
    int action_fd = -1;
    int status;

    status = validate_parent(parent_dirfd, &parent_stat);
    if (status != CPL_OK) {
        return status;
    }
    status = validate_regular_fd(fd, parent_stat.st_dev, &journal_stat);
    if (status != CPL_OK) {
        return status;
    }
    status = validate_path_pair(parent_dirfd, journal_name, &journal_stat);
    if (status != CPL_OK) {
        return status;
    }
    status = open_lock_at(parent_dirfd, journal_name, ".append.lock",
        parent_stat.st_dev, &append_fd);
    if (status != CPL_OK) {
        return status;
    }
    status = open_lock_at(parent_dirfd, journal_name, ".action.lock",
        parent_stat.st_dev, &action_fd);
    if (status != CPL_OK) {
        (void)close(append_fd);
        return status;
    }
    journal = calloc(1U, sizeof(*journal));
    if (journal == NULL) {
        (void)close(action_fd);
        (void)close(append_fd);
        return CPL_ERR_SYSTEM;
    }
    journal->fd = fd;
    journal->append_lock_fd = append_fd;
    journal->action_lock_fd = action_fd;
    journal->owner_pid = getpid();
    journal->parent_dev = parent_stat.st_dev;
    journal->parent_ino = parent_stat.st_ino;
    journal->journal_dev = journal_stat.st_dev;
    journal->journal_ino = journal_stat.st_ino;
    journal->normal_limit = normal_limit;
    journal->hard_limit = hard_limit;
    (void)memcpy(journal->nonce, nonce, CPL_HASH_SIZE);
    (void)memcpy(journal->journal_name, journal_name, strlen(journal_name) + 1U);
    if (pthread_mutex_init(&journal->append_mutex, NULL) != 0) {
        free(journal);
        (void)close(action_fd);
        (void)close(append_fd);
        return CPL_ERR_SYSTEM;
    }
    if (pthread_mutex_init(&journal->action_mutex, NULL) != 0) {
        (void)pthread_mutex_destroy(&journal->append_mutex);
        free(journal);
        (void)close(action_fd);
        (void)close(append_fd);
        return CPL_ERR_SYSTEM;
    }
    *out = journal;
    return CPL_OK;
}

static int ensure_owner(cpl_journal *journal) {
    if (journal == NULL) {
        return CPL_ERR_CLOSED;
    }
    if (journal->owner_pid != getpid()) {
        return CPL_ERR_FORK_INHERITED;
    }
    return CPL_OK;
}

static int encode_record(const cpl_journal *journal,
    const struct cpl_record *record, uint64_t sequence,
    const uint8_t previous[CPL_HASH_SIZE], uint8_t *encoded,
    size_t encoded_capacity, size_t *encoded_length,
    uint8_t out_hash[CPL_HASH_SIZE]) {
    size_t total = CPL_HEADER_SIZE + sizeof(*record);
    uint32_t checksum;

    if (encoded_capacity < total || total > UINT32_MAX) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(encoded, 0, total);
    (void)memcpy(encoded, CPL_MAGIC, CPL_MAGIC_SIZE);
    put_u16(encoded + 8U, CPL_FORMAT_VERSION);
    put_u16(encoded + 10U, CPL_HEADER_SIZE);
    put_u32(encoded + 12U, (uint32_t)total);
    (void)memcpy(encoded + 16U, journal->nonce, CPL_HASH_SIZE);
    put_u64(encoded + 48U, sequence);
    put_u64(encoded + 56U, record->cleanup_epoch);
    (void)memcpy(encoded + 64U, previous, CPL_HASH_SIZE);
    put_u32(encoded + 96U, record->kind);
    put_u32(encoded + 100U, (uint32_t)sizeof(*record));
    put_u32(encoded + 104U, 0U);
    (void)memcpy(encoded + CPL_HEADER_SIZE, record, sizeof(*record));
    checksum = crc32c(encoded, total);
    put_u32(encoded + 104U, checksum);
    sha256(encoded, total, out_hash);
    *encoded_length = total;
    return CPL_OK;
}

static void state_no_generation(struct cpl_state *state) {
    (void)memset(state, 0, sizeof(*state));
    state->kind = CPL_STATE_NO_GENERATION;
}

static void state_from_record(struct cpl_state *out,
    const struct cpl_record *record, uint32_t kind) {
    (void)memset(out, 0, sizeof(*out));
    out->kind = kind;
    out->generation = record->generation;
    out->cleanup_epoch = record->cleanup_epoch;
    out->authority_epoch = record->authority_epoch;
    out->deadline_ns = record->deadline_ns;
    out->lease_deadline_ns = record->lease_deadline_ns;
    out->completed_steps = record->completed_steps;
    out->process_pid = record->process_pid;
    out->process_start_ns = record->process_start_ns;
    out->process_uid = record->process_uid;
    out->process_pgid = record->process_pgid;
    out->process_sid = record->process_sid;
    out->process_identity_flags = record->process_identity_flags;
    out->executable_dev = record->executable_dev;
    out->executable_ino = record->executable_ino;
    out->batch_outcome = record->batch_outcome;
    (void)memcpy(out->boot_id, record->boot_id, CPL_HASH_SIZE);
    (void)memcpy(out->executable_hash, record->executable_hash,
        CPL_HASH_SIZE);
    (void)memcpy(out->candidate, record->candidate, CPL_ID_SIZE);
    (void)memcpy(out->executor, record->executor, CPL_ID_SIZE);
    (void)memcpy(out->prior_actor, record->prior_actor, CPL_ID_SIZE);
    (void)memcpy(out->authority, record->authority, CPL_ID_SIZE);
    (void)memcpy(out->exact_batch, record->exact_batch, CPL_ID_SIZE);
    (void)memcpy(out->inherited_batch, record->inherited_batch, CPL_ID_SIZE);
    (void)memcpy(out->reason, record->reason, CPL_REASON_SIZE);
}

static void preserve_process_identity(struct cpl_state *out,
    const struct cpl_state *current) {
    out->process_pid = current->process_pid;
    out->process_start_ns = current->process_start_ns;
    out->process_uid = current->process_uid;
    out->process_pgid = current->process_pgid;
    out->process_sid = current->process_sid;
    out->process_identity_flags = current->process_identity_flags;
    out->executable_dev = current->executable_dev;
    out->executable_ino = current->executable_ino;
    (void)memcpy(out->boot_id, current->boot_id, CPL_HASH_SIZE);
    (void)memcpy(out->executable_hash, current->executable_hash,
        CPL_HASH_SIZE);
}

int cpl_lifecycle_apply(const struct cpl_state *current,
    const struct cpl_record *record, struct cpl_state *out) {
    struct cpl_state next;

    if (current == NULL || record == NULL || out == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(out, 0, sizeof(*out));
    if (current->kind == CPL_STATE_DONE ||
        current->kind == CPL_STATE_UNCONFIRMED) {
        return CPL_ERR_ILLEGAL_TRANSITION;
    }

    switch (record->kind) {
    case CPL_RECORD_PREPARED:
        if (!has_id(record->candidate) || record->generation == 0U) {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        if (current->kind == CPL_STATE_NO_GENERATION) {
            state_from_record(&next, record, CPL_STATE_PREPARED);
        } else if (current->kind == CPL_STATE_RETIRING_IDLE &&
            record->generation == current->generation + 1U) {
            state_from_record(&next, record, CPL_STATE_PREPARED);
            (void)memcpy(next.inherited_batch, current->exact_batch,
                CPL_ID_SIZE);
        } else {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        break;
    case CPL_RECORD_ACTIVE_READY:
        if (current->kind != CPL_STATE_PREPARED ||
            record->generation != current->generation ||
            !same_id(record->executor, current->candidate)) {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        state_from_record(&next, record, CPL_STATE_ACTIVE_READY);
        (void)memcpy(next.inherited_batch, current->inherited_batch,
            CPL_ID_SIZE);
        break;
    case CPL_RECORD_BATCH_ACTIVE:
        if (current->kind != CPL_STATE_ACTIVE_READY ||
            record->generation != current->generation ||
            !same_id(record->executor, current->executor) ||
            !has_id(record->exact_batch) ||
            record->completed_steps != current->completed_steps) {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        state_from_record(&next, record, CPL_STATE_BATCH_ACTIVE);
        preserve_process_identity(&next, current);
        (void)memcpy(next.inherited_batch, current->inherited_batch,
            CPL_ID_SIZE);
        break;
    case CPL_RECORD_BATCH_DONE:
        if (record->generation != current->generation ||
            !same_id(record->executor, current->executor) ||
            !same_id(record->exact_batch, current->exact_batch) ||
            record->completed_steps < current->completed_steps) {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        if (current->kind == CPL_STATE_BATCH_ACTIVE &&
            record->batch_outcome == CPL_BATCH_NONE) {
            state_from_record(&next, record, CPL_STATE_ACTIVE_READY);
            preserve_process_identity(&next, current);
            (void)memcpy(next.inherited_batch, current->inherited_batch,
                CPL_ID_SIZE);
            (void)memset(next.exact_batch, 0, CPL_ID_SIZE);
        } else if (current->kind == CPL_STATE_RETIRING_BATCH &&
            (record->batch_outcome == CPL_BATCH_COMPLETED ||
             record->batch_outcome == CPL_BATCH_INTERRUPTED)) {
            next = *current;
            next.kind = CPL_STATE_RETIRING_IDLE;
            next.batch_outcome = record->batch_outcome;
            next.completed_steps = record->completed_steps;
        } else {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        break;
    case CPL_RECORD_RETIRING_IDLE:
        if (record->generation != current->generation ||
            !has_id(record->authority) ||
            record->authority_epoch <= current->authority_epoch) {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        if (current->kind == CPL_STATE_PREPARED &&
            same_id(record->prior_actor, current->candidate)) {
            state_from_record(&next, record, CPL_STATE_RETIRING_IDLE);
        } else if (current->kind == CPL_STATE_ACTIVE_READY &&
            same_id(record->prior_actor, current->executor)) {
            state_from_record(&next, record, CPL_STATE_RETIRING_IDLE);
            next.completed_steps = current->completed_steps;
            preserve_process_identity(&next, current);
        } else {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        break;
    case CPL_RECORD_RETIRING_BATCH:
        if (current->kind != CPL_STATE_BATCH_ACTIVE ||
            record->generation != current->generation ||
            !same_id(record->prior_actor, current->executor) ||
            !same_id(record->exact_batch, current->exact_batch) ||
            !has_id(record->authority) ||
            record->authority_epoch <= current->authority_epoch) {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        state_from_record(&next, record, CPL_STATE_RETIRING_BATCH);
        next.completed_steps = current->completed_steps;
        preserve_process_identity(&next, current);
        (void)memcpy(next.executor, current->executor, CPL_ID_SIZE);
        break;
    case CPL_RECORD_REPLACE_AUTHORITY:
        if ((current->kind != CPL_STATE_RETIRING_IDLE &&
             current->kind != CPL_STATE_RETIRING_BATCH) ||
            record->authority_epoch != current->authority_epoch + 1U ||
            !has_id(record->authority)) {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        next = *current;
        next.authority_epoch = record->authority_epoch;
        next.deadline_ns = record->deadline_ns;
        (void)memcpy(next.authority, record->authority, CPL_ID_SIZE);
        break;
    case CPL_RECORD_DONE:
        if (current->kind != CPL_STATE_ACTIVE_READY ||
            record->generation != current->generation ||
            !same_id(record->executor, current->executor) ||
            current->completed_steps != CPL_ALL_COMPLETED_STEPS) {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        next = *current;
        next.kind = CPL_STATE_DONE;
        break;
    case CPL_RECORD_UNCONFIRMED:
        if (is_zero(record->reason, CPL_REASON_SIZE)) {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        next = *current;
        next.retained_kind = current->kind;
        next.kind = CPL_STATE_UNCONFIRMED;
        (void)memcpy(next.reason, record->reason, CPL_REASON_SIZE);
        break;
    default:
        return CPL_ERR_ILLEGAL_TRANSITION;
    }
    *out = next;
    return CPL_OK;
}

static int scan_internal(cpl_journal *journal, struct cpl_chain *out) {
    struct stat file_stat;
    uint8_t *bytes = NULL;
    uint64_t readable;
    uint64_t offset = 0U;
    ssize_t got;

    (void)memset(out, 0, sizeof(*out));
    state_no_generation(&out->state);
    if (fstat(journal->fd, &file_stat) < 0 || file_stat.st_size < 0) {
        return CPL_ERR_SYSTEM;
    }
    out->physical_eof = (uint64_t)file_stat.st_size;
    out->unhealthy = journal->unhealthy || out->physical_eof > journal->hard_limit;
    readable = out->physical_eof;
    if (readable > journal->hard_limit) {
        readable = journal->hard_limit;
    }
    if (readable == 0U) {
        return CPL_OK;
    }
    if (readable > SIZE_MAX) {
        return CPL_ERR_CORRUPT;
    }
    bytes = malloc((size_t)readable);
    if (bytes == NULL) {
        return CPL_ERR_SYSTEM;
    }
    got = pread(journal->fd, bytes, (size_t)readable, 0);
    if (got < 0 || (uint64_t)got != readable) {
        free(bytes);
        return CPL_ERR_SYSTEM;
    }
    while (offset < readable) {
        uint64_t remaining = readable - offset;
        uint8_t *candidate = bytes + offset;
        uint32_t total;
        uint32_t payload_length;
        uint32_t stored_crc;
        uint32_t computed_crc;
        uint64_t sequence;
        uint8_t hash[CPL_HASH_SIZE];
        uint8_t saved_crc[4];
        struct cpl_record record;
        struct cpl_state next;
        int transition;

        if (remaining < CPL_HEADER_SIZE) {
            out->invalid_bytes += remaining;
            break;
        }
        if (memcmp(candidate, CPL_MAGIC, CPL_MAGIC_SIZE) != 0) {
            ++out->invalid_bytes;
            ++offset;
            continue;
        }
        total = get_u32(candidate + 12U);
        payload_length = get_u32(candidate + 100U);
        if (get_u16(candidate + 8U) != CPL_FORMAT_VERSION ||
            get_u16(candidate + 10U) != CPL_HEADER_SIZE ||
            total != CPL_HEADER_SIZE + sizeof(struct cpl_record) ||
            payload_length != sizeof(struct cpl_record) || total > remaining ||
            memcmp(candidate + 16U, journal->nonce, CPL_HASH_SIZE) != 0) {
            ++out->invalid_bytes;
            ++offset;
            continue;
        }
        (void)memcpy(saved_crc, candidate + 104U, sizeof(saved_crc));
        stored_crc = get_u32(saved_crc);
        (void)memset(candidate + 104U, 0, 4U);
        computed_crc = crc32c(candidate, total);
        (void)memcpy(candidate + 104U, saved_crc, sizeof(saved_crc));
        if (stored_crc != computed_crc) {
            ++out->invalid_bytes;
            ++offset;
            continue;
        }
        sha256(candidate, total, hash);
        (void)memcpy(&record, candidate + CPL_HEADER_SIZE, sizeof(record));
        sequence = get_u64(candidate + 48U);
        if (!out->has_intent) {
            if (sequence == 0U && record.kind == CPL_RECORD_INTENT &&
                is_zero(candidate + 64U, CPL_HASH_SIZE)) {
                out->has_intent = true;
                out->head_sequence = 0U;
                out->canonical_records = 1U;
                (void)memcpy(out->head_hash, hash, CPL_HASH_SIZE);
            } else {
                ++out->stale_records;
            }
            offset += total;
            continue;
        }
        if (sequence != out->head_sequence + 1U ||
            memcmp(candidate + 64U, out->head_hash, CPL_HASH_SIZE) != 0) {
            ++out->stale_records;
            offset += total;
            continue;
        }
        transition = cpl_lifecycle_apply(&out->state, &record, &next);
        if (transition != CPL_OK) {
            ++out->stale_records;
            offset += total;
            continue;
        }
        out->state = next;
        out->head_sequence = sequence;
        ++out->canonical_records;
        (void)memcpy(out->head_hash, hash, CPL_HASH_SIZE);
        offset += total;
    }
    if (out->physical_eof > readable) {
        out->invalid_bytes += out->physical_eof - readable;
    }
    free(bytes);
    return CPL_OK;
}

#ifdef CPL_ENABLE_FAULT_INJECTION
static int certify_notify_fd = -1;
static int certify_wait_fd = -1;

static void fault_exit(const char *point) {
    const char *selected = getenv("CPL_FAULT_POINT");

    if (selected != NULL && strcmp(selected, point) == 0) {
        _exit(91);
    }
}

static int checked_byte_write(int fd) {
    uint8_t byte = (uint8_t)'1';

    return write(fd, &byte, 1U) == 1 ? CPL_OK : CPL_ERR_SYSTEM;
}

static int checked_byte_read(int fd) {
    uint8_t byte;

    return read(fd, &byte, 1U) == 1 ? CPL_OK : CPL_ERR_SYSTEM;
}

int cpl_fault_configure_certify_pause(int notify_fd, int wait_fd) {
    if (notify_fd < 0 || wait_fd < 0) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    certify_notify_fd = notify_fd;
    certify_wait_fd = wait_fd;
    return CPL_OK;
}

static int fault_certify_pause(void) {
    int status;

    if (certify_notify_fd < 0 || certify_wait_fd < 0) {
        return CPL_OK;
    }
    status = checked_byte_write(certify_notify_fd);
    if (status == CPL_OK) {
        status = checked_byte_read(certify_wait_fd);
    }
    certify_notify_fd = -1;
    certify_wait_fd = -1;
    return status;
}
#else
static void fault_exit(const char *point) {
    (void)point;
}

static int fault_certify_pause(void) {
    return CPL_OK;
}
#endif

static int preallocate_file(int fd, uint64_t length) {
    struct fstore allocation;

    if (length > (uint64_t)INT64_MAX) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(&allocation, 0, sizeof(allocation));
    allocation.fst_flags = F_ALLOCATECONTIG;
    allocation.fst_posmode = F_PEOFPOSMODE;
    allocation.fst_length = (off_t)length;
    if (fcntl(fd, F_PREALLOCATE, &allocation) == 0) {
        return CPL_OK;
    }
    allocation.fst_flags = F_ALLOCATEALL;
    if (fcntl(fd, F_PREALLOCATE, &allocation) < 0) {
        return CPL_ERR_SYSTEM;
    }
    return CPL_OK;
}

int cpl_journal_create_at(int parent_dirfd, const char *journal_name,
    const uint8_t nonce[CPL_HASH_SIZE], uint64_t normal_limit,
    uint64_t hard_limit, cpl_journal **out,
    struct cpl_create_receipt *receipt) {
    struct stat parent_stat;
    struct cpl_record intent;
    cpl_journal *journal = NULL;
    uint8_t encoded[CPL_HEADER_SIZE + sizeof(struct cpl_record)];
    uint8_t zero_hash[CPL_HASH_SIZE] = {0};
    uint8_t intent_hash[CPL_HASH_SIZE];
    size_t encoded_length = 0U;
    ssize_t written;
    int fd;
    int status;
    uint64_t deadline;
    bool mutex_held = false;
    bool flock_held = false;

    if (out == NULL || receipt == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    *out = NULL;
    (void)memset(receipt, 0, sizeof(*receipt));
    status = validate_component(journal_name);
    if (status != CPL_OK || nonce == NULL || normal_limit >= hard_limit ||
        normal_limit < sizeof(encoded) || hard_limit < sizeof(encoded)) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    status = validate_parent(parent_dirfd, &parent_stat);
    if (status != CPL_OK) {
        return status;
    }
    fd = openat(parent_dirfd, journal_name,
        O_RDWR | O_CREAT | O_EXCL | O_APPEND | O_NOFOLLOW | O_CLOEXEC,
        (mode_t)0600);
    if (fd < 0) {
        if (errno == EEXIST) {
            return CPL_ERR_EXISTS;
        }
        if (errno == ELOOP) {
            return CPL_ERR_SYMLINK;
        }
        return CPL_ERR_SYSTEM;
    }
    fault_exit("after_openat");
    status = initialize_handle(fd, parent_dirfd, journal_name, nonce,
        normal_limit, hard_limit, &journal);
    if (status != CPL_OK) {
        (void)close(fd);
        return status;
    }
    status = preallocate_file(fd, hard_limit);
    if (status != CPL_OK) {
        cpl_journal_close(journal);
        return status;
    }
    fault_exit("after_preallocate");
    (void)memset(&intent, 0, sizeof(intent));
    intent.kind = CPL_RECORD_INTENT;
    status = encode_record(journal, &intent, 0U, zero_hash, encoded,
        sizeof(encoded), &encoded_length, intent_hash);
    if (status != CPL_OK) {
        cpl_journal_close(journal);
        return status;
    }
    deadline = effective_deadline(0U);
    status = take_mutex(&journal->append_mutex, deadline);
    if (status == CPL_OK) {
        mutex_held = true;
        status = take_flock(journal->append_lock_fd, deadline);
        if (status == CPL_OK) {
            flock_held = true;
        }
    }
    if (status != CPL_OK) {
        if (flock_held) {
            (void)release_flock(journal->append_lock_fd);
        }
        if (mutex_held) {
            (void)pthread_mutex_unlock(&journal->append_mutex);
        }
        cpl_journal_close(journal);
        return status;
    }
    written = write(fd, encoded, encoded_length);
    (void)release_flock(journal->append_lock_fd);
    (void)pthread_mutex_unlock(&journal->append_mutex);
    if (written < 0 || (size_t)written != encoded_length) {
        journal->unhealthy = true;
        cpl_journal_close(journal);
        return CPL_ERR_IO_SHORT;
    }
    fault_exit("after_intent_append");
    if (fcntl(fd, F_FULLFSYNC) < 0) {
        cpl_journal_close(journal);
        return CPL_ERR_SYSTEM;
    }
    fault_exit("after_journal_fullfsync");
    status = validate_parent_identity(journal, parent_dirfd);
    if (status != CPL_OK || fsync(parent_dirfd) < 0) {
        cpl_journal_close(journal);
        return status == CPL_OK ? CPL_ERR_SYSTEM : status;
    }
    fault_exit("after_intent_parent_fsync");
    receipt->state = CPL_INTENT_PARENT_DIRSYNCED;
    (void)memcpy(receipt->intent_hash, intent_hash, CPL_HASH_SIZE);
    *out = journal;
    return CPL_OK;
}

static int first_record_nonce_status(int fd,
    const uint8_t nonce[CPL_HASH_SIZE]) {
    uint8_t header[CPL_HEADER_SIZE];
    ssize_t got = pread(fd, header, sizeof(header), 0);

    if (got < 0) {
        return CPL_ERR_SYSTEM;
    }
    if ((size_t)got >= 48U &&
        memcmp(header, CPL_MAGIC, CPL_MAGIC_SIZE) == 0 &&
        memcmp(header + 16U, nonce, CPL_HASH_SIZE) != 0) {
        return CPL_ERR_NONCE_MISMATCH;
    }
    return CPL_OK;
}

int cpl_journal_open_at(int parent_dirfd, const char *journal_name,
    const uint8_t nonce[CPL_HASH_SIZE], uint64_t normal_limit,
    uint64_t hard_limit, cpl_journal **out) {
    cpl_journal *journal = NULL;
    struct cpl_chain chain;
    int fd;
    int status;

    if (out == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    *out = NULL;
    status = validate_component(journal_name);
    if (status != CPL_OK || nonce == NULL || normal_limit >= hard_limit ||
        normal_limit < CPL_HEADER_SIZE + sizeof(struct cpl_record)) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    fd = openat(parent_dirfd, journal_name,
        O_RDWR | O_APPEND | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) {
        if (errno == ELOOP) {
            return CPL_ERR_SYMLINK;
        }
        if (errno == ENOENT) {
            return CPL_ERR_NOT_FOUND;
        }
        return CPL_ERR_SYSTEM;
    }
    status = initialize_handle(fd, parent_dirfd, journal_name, nonce,
        normal_limit, hard_limit, &journal);
    if (status == CPL_OK) {
        status = first_record_nonce_status(fd, nonce);
    }
    if (status == CPL_OK) {
        status = scan_internal(journal, &chain);
    }
    if (status != CPL_OK) {
        if (journal != NULL) {
            cpl_journal_close(journal);
        } else {
            (void)close(fd);
        }
        return status;
    }
    *out = journal;
    return CPL_OK;
}

int cpl_journal_scan(cpl_journal *journal, struct cpl_chain *out) {
    int status = ensure_owner(journal);

    if (status != CPL_OK) {
        return status;
    }
    if (out == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    return scan_internal(journal, out);
}

static bool recovery_allowed(const struct cpl_chain *chain,
    const struct cpl_record *record) {
    if (record->kind == CPL_RECORD_RETIRING_IDLE ||
        record->kind == CPL_RECORD_RETIRING_BATCH ||
        record->kind == CPL_RECORD_BATCH_DONE ||
        record->kind == CPL_RECORD_UNCONFIRMED ||
        record->kind == CPL_RECORD_REPLACE_AUTHORITY) {
        return true;
    }
    return record->kind == CPL_RECORD_PREPARED &&
        chain->state.kind == CPL_STATE_RETIRING_IDLE;
}

int cpl_journal_append(cpl_journal *journal, const uint8_t *record_bytes,
    uint32_t record_len, uint32_t record_class, uint64_t deadline_ns,
    uint8_t out_hash[CPL_HASH_SIZE]) {
    struct cpl_chain chain;
    struct cpl_state next;
    struct cpl_record record;
    uint8_t encoded[CPL_HEADER_SIZE + sizeof(struct cpl_record)];
    size_t encoded_length = 0U;
    uint64_t deadline = effective_deadline(deadline_ns);
    uint64_t maximum_write = sizeof(encoded);
    ssize_t written;
    int status;
    bool mutex_held = false;
    bool flock_held = false;

    if (record_bytes == NULL || out_hash == NULL ||
        record_len != sizeof(record) ||
        (record_class != CPL_RECORD_NORMAL &&
         record_class != CPL_RECORD_RECOVERY)) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    status = ensure_owner(journal);
    if (status != CPL_OK) {
        return status;
    }
    (void)memcpy(&record, record_bytes, sizeof(record));
    status = take_mutex(&journal->append_mutex, deadline);
    if (status != CPL_OK) {
        return status;
    }
    mutex_held = true;
    status = take_flock(journal->append_lock_fd, deadline);
    if (status != CPL_OK) {
        goto done;
    }
    flock_held = true;
    status = scan_internal(journal, &chain);
    if (status != CPL_OK) {
        goto done;
    }
    if (!chain.has_intent || chain.unhealthy) {
        status = CPL_ERR_CORRUPT;
        goto done;
    }
    if (!is_zero(record.parent_hash, CPL_HASH_SIZE) &&
        memcmp(record.parent_hash, chain.head_hash, CPL_HASH_SIZE) != 0) {
        status = CPL_ERR_PARENT_MISMATCH;
        goto done;
    }
    status = cpl_lifecycle_apply(&chain.state, &record, &next);
    if (status != CPL_OK) {
        goto done;
    }
    if (record_class == CPL_RECORD_RECOVERY &&
        !recovery_allowed(&chain, &record)) {
        status = CPL_ERR_RECORD_CLASS;
        goto done;
    }
    if (chain.physical_eof > journal->hard_limit ||
        maximum_write > journal->hard_limit - chain.physical_eof) {
        journal->unhealthy = true;
        status = CPL_ERR_HARD_LIMIT;
        goto done;
    }
    if (record_class == CPL_RECORD_NORMAL &&
        (chain.physical_eof > journal->normal_limit ||
         maximum_write > journal->normal_limit - chain.physical_eof)) {
        status = CPL_ERR_NORMAL_LIMIT;
        goto done;
    }
    (void)memcpy(record.parent_hash, chain.head_hash, CPL_HASH_SIZE);
    status = encode_record(journal, &record, chain.head_sequence + 1U,
        chain.head_hash, encoded, sizeof(encoded), &encoded_length, out_hash);
    if (status != CPL_OK) {
        goto done;
    }
    written = write(journal->fd, encoded, encoded_length);
    if (written < 0 || (size_t)written != encoded_length) {
        journal->unhealthy = true;
        status = CPL_ERR_IO_SHORT;
        goto done;
    }
    status = CPL_OK;

done:
    if (flock_held && release_flock(journal->append_lock_fd) != CPL_OK &&
        status == CPL_OK) {
        status = CPL_ERR_SYSTEM;
    }
    if (mutex_held && pthread_mutex_unlock(&journal->append_mutex) != 0 &&
        status == CPL_OK) {
        status = CPL_ERR_SYSTEM;
    }
    return status;
}

static bool process_absent(int64_t pid) {
    if (pid <= 0) {
        return true;
    }
    if (pid > INT_MAX) {
        return false;
    }
    if (kill((pid_t)pid, 0) == 0 || errno == EPERM) {
        return false;
    }
    return errno == ESRCH;
}

int cpl_journal_certify(cpl_journal *journal, uint64_t deadline_ns,
    struct cpl_certified_head *out) {
    struct cpl_chain snapshot;
    struct cpl_chain verified;
    uint64_t deadline = effective_deadline(deadline_ns);
    uint32_t attempts = 0U;
    int status;

    if (out == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(out, 0, sizeof(*out));
    status = ensure_owner(journal);
    if (status != CPL_OK) {
        return status;
    }
    for (;;) {
        bool flock_held = false;

        ++attempts;
        status = take_mutex(&journal->append_mutex, deadline);
        if (status != CPL_OK) {
            return status;
        }
        status = take_flock(journal->append_lock_fd, deadline);
        if (status == CPL_OK) {
            flock_held = true;
            status = scan_internal(journal, &snapshot);
        }
        if (flock_held && release_flock(journal->append_lock_fd) != CPL_OK &&
            status == CPL_OK) {
            status = CPL_ERR_SYSTEM;
        }
        (void)pthread_mutex_unlock(&journal->append_mutex);
        if (status != CPL_OK) {
            return status;
        }
        status = fault_certify_pause();
        if (status != CPL_OK) {
            return status;
        }
        if (fcntl(journal->fd, F_FULLFSYNC) < 0) {
            return CPL_ERR_SYSTEM;
        }
        status = take_mutex(&journal->append_mutex, deadline);
        if (status != CPL_OK) {
            return status;
        }
        status = take_flock(journal->append_lock_fd, deadline);
        flock_held = false;
        if (status == CPL_OK) {
            flock_held = true;
            status = scan_internal(journal, &verified);
        }
        if (flock_held && release_flock(journal->append_lock_fd) != CPL_OK &&
            status == CPL_OK) {
            status = CPL_ERR_SYSTEM;
        }
        (void)pthread_mutex_unlock(&journal->append_mutex);
        if (status != CPL_OK) {
            return status;
        }
        if (snapshot.physical_eof == verified.physical_eof &&
            memcmp(snapshot.head_hash, verified.head_hash, CPL_HASH_SIZE) == 0) {
            if (verified.state.kind == CPL_STATE_DONE &&
                !process_absent(verified.state.process_pid)) {
                return CPL_ERR_PROCESS_PRESENT;
            }
            out->state = verified.state;
            (void)memcpy(out->head_hash, verified.head_hash, CPL_HASH_SIZE);
            out->head_sequence = verified.head_sequence;
            out->physical_eof = verified.physical_eof;
            out->attempts = attempts;
            out->has_intent = verified.has_intent;
            out->done_authority = verified.has_intent &&
                verified.state.kind == CPL_STATE_DONE &&
                verified.state.completed_steps == CPL_ALL_COMPLETED_STEPS;
            out->partial_create_authority = !verified.has_intent;
            return CPL_OK;
        }
        if (monotonic_ns() >= deadline) {
            return CPL_ERR_CERTIFY_TIMEOUT;
        }
    }
}

static int validate_parent_identity(cpl_journal *journal, int parent_dirfd) {
    struct stat parent_stat;
    int status = validate_parent(parent_dirfd, &parent_stat);

    if (status != CPL_OK) {
        return status;
    }
    if (parent_stat.st_dev != journal->parent_dev ||
        parent_stat.st_ino != journal->parent_ino) {
        return CPL_ERR_IDENTITY_DRIFT;
    }
    return CPL_OK;
}

static int validate_authority(cpl_journal *journal,
    const struct cpl_delete_authority *authority, uint64_t deadline,
    struct cpl_certified_head *certified) {
    int status;

    if (authority == NULL ||
        memcmp(authority->allocation_nonce, journal->nonce,
            CPL_HASH_SIZE) != 0) {
        return CPL_ERR_AUTHORITY;
    }
    status = cpl_journal_certify(journal, deadline, certified);
    if (status != CPL_OK) {
        return status;
    }
    if (memcmp(authority->certified_hash, certified->head_hash,
            CPL_HASH_SIZE) != 0) {
        return CPL_ERR_AUTHORITY;
    }
    if (authority->kind == CPL_DELETE_CERTIFIED_DONE) {
        return certified->done_authority ? CPL_OK : CPL_ERR_AUTHORITY;
    }
    if (authority->kind == CPL_DELETE_UNRELEASED_PARTIAL_CREATE) {
        return certified->partial_create_authority ? CPL_OK : CPL_ERR_AUTHORITY;
    }
    return CPL_ERR_AUTHORITY;
}

static int require_workdir_absent(int parent_dirfd, const char *name) {
    struct stat parent_stat;
    struct stat workdir_stat;
    int status;

    if (validate_component(name) != CPL_OK) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    status = validate_parent(parent_dirfd, &parent_stat);
    if (status != CPL_OK) {
        return status;
    }
    if (fstatat(parent_dirfd, name, &workdir_stat, AT_SYMLINK_NOFOLLOW) == 0) {
        return CPL_ERR_WORKDIR_PRESENT;
    }
    if (errno != ENOENT) {
        return CPL_ERR_SYSTEM;
    }
    return CPL_OK;
}

static int validate_open_journal_identity(cpl_journal *journal,
    int parent_dirfd, const char *journal_name, bool require_absent) {
    struct stat journal_stat;
    struct stat path_stat;
    int status;

    if (strcmp(journal->journal_name, journal_name) != 0) {
        return CPL_ERR_IDENTITY_DRIFT;
    }
    status = validate_parent_identity(journal, parent_dirfd);
    if (status != CPL_OK) {
        return status;
    }
    if (fstat(journal->fd, &journal_stat) < 0) {
        return CPL_ERR_SYSTEM;
    }
    if (journal_stat.st_dev != journal->journal_dev ||
        journal_stat.st_ino != journal->journal_ino) {
        return CPL_ERR_IDENTITY_DRIFT;
    }
    if (fstatat(parent_dirfd, journal_name, &path_stat,
            AT_SYMLINK_NOFOLLOW) < 0) {
        if (errno == ENOENT) {
            return require_absent ? CPL_OK : CPL_ERR_NOT_FOUND;
        }
        return CPL_ERR_SYSTEM;
    }
    if (require_absent) {
        return CPL_ERR_NOT_ABSENT;
    }
    if (S_ISLNK(path_stat.st_mode)) {
        return CPL_ERR_SYMLINK;
    }
    if (path_stat.st_dev != journal_stat.st_dev ||
        path_stat.st_ino != journal_stat.st_ino) {
        return CPL_ERR_IDENTITY_DRIFT;
    }
    return CPL_OK;
}

static int lock_action(cpl_journal *journal, uint64_t deadline) {
    int status = take_mutex(&journal->action_mutex, deadline);

    if (status != CPL_OK) {
        return status;
    }
    status = take_flock(journal->action_lock_fd, deadline);
    if (status != CPL_OK) {
        (void)pthread_mutex_unlock(&journal->action_mutex);
    }
    return status;
}

static void unlock_action(cpl_journal *journal) {
    (void)release_flock(journal->action_lock_fd);
    (void)pthread_mutex_unlock(&journal->action_mutex);
}

static int lock_certified_head(cpl_journal *journal,
    const struct cpl_certified_head *certified, uint64_t deadline) {
    struct cpl_chain current;
    int status = take_mutex(&journal->append_mutex, deadline);

    if (status != CPL_OK) {
        return status;
    }
    status = take_flock(journal->append_lock_fd, deadline);
    if (status != CPL_OK) {
        (void)pthread_mutex_unlock(&journal->append_mutex);
        return status;
    }
    status = scan_internal(journal, &current);
    if (status != CPL_OK ||
        current.physical_eof != certified->physical_eof ||
        memcmp(current.head_hash, certified->head_hash, CPL_HASH_SIZE) != 0) {
        (void)release_flock(journal->append_lock_fd);
        (void)pthread_mutex_unlock(&journal->append_mutex);
        return status == CPL_OK ? CPL_ERR_AUTHORITY : status;
    }
    return CPL_OK;
}

static void unlock_certified_head(cpl_journal *journal) {
    (void)release_flock(journal->append_lock_fd);
    (void)pthread_mutex_unlock(&journal->append_mutex);
}

static void successful_delete_close(cpl_journal **inout_j,
    struct cpl_delete_receipt *receipt) {
    cpl_journal *journal = *inout_j;

    receipt->state = CPL_JOURNAL_UNLINK_PARENT_DIRSYNCED;
    receipt->slot_releasable = true;
    *inout_j = NULL;
    cpl_journal_close(journal);
}

int cpl_journal_delete_at(cpl_journal **inout_j, int parent_dirfd,
    const char *journal_name, int workdir_parent_dirfd,
    const char *workdir_name, const struct cpl_delete_authority *authority,
    uint64_t deadline_ns, struct cpl_delete_receipt *receipt) {
    struct cpl_certified_head certified;
    cpl_journal *journal;
    uint64_t deadline = effective_deadline(deadline_ns);
    int status;
    bool head_locked = false;

    if (receipt == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(receipt, 0, sizeof(*receipt));
    if (inout_j == NULL || *inout_j == NULL ||
        validate_component(journal_name) != CPL_OK) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    journal = *inout_j;
    status = ensure_owner(journal);
    if (status != CPL_OK) {
        return status;
    }
    status = validate_parent_identity(journal, parent_dirfd);
    if (status != CPL_OK) {
        return status;
    }
    status = lock_action(journal, deadline);
    if (status != CPL_OK) {
        return status;
    }
    status = validate_authority(journal, authority, deadline, &certified);
    if (status != CPL_OK) {
        goto done;
    }
    status = lock_certified_head(journal, &certified, deadline);
    if (status != CPL_OK) {
        goto done;
    }
    head_locked = true;
    fault_exit("after_authority_revalidated");
    if (authority->kind == CPL_DELETE_CERTIFIED_DONE) {
        if (!process_absent(certified.state.process_pid)) {
            status = CPL_ERR_PROCESS_PRESENT;
            goto done;
        }
        fault_exit("after_process_absence_verified");
    }
    status = require_workdir_absent(workdir_parent_dirfd, workdir_name);
    if (status != CPL_OK) {
        goto done;
    }
    status = validate_parent_identity(journal, workdir_parent_dirfd);
    if (status != CPL_OK) {
        goto done;
    }
    fault_exit("after_workdir_absence_verified");
    status = validate_open_journal_identity(journal, parent_dirfd,
        journal_name, false);
    if (status != CPL_OK) {
        goto done;
    }
    if (unlinkat(parent_dirfd, journal_name, 0) < 0) {
        status = CPL_ERR_SYSTEM;
        goto done;
    }
    fault_exit("after_journal_unlinkat");
    status = validate_parent_identity(journal, parent_dirfd);
    if (status != CPL_OK || fsync(parent_dirfd) < 0) {
        if (status == CPL_OK) {
            status = CPL_ERR_SYSTEM;
        }
        goto done;
    }
    fault_exit("after_journal_unlink_parent_fsync");
    status = CPL_OK;

done:
    if (head_locked) {
        unlock_certified_head(journal);
    }
    unlock_action(journal);
    if (status == CPL_OK) {
        successful_delete_close(inout_j, receipt);
    }
    return status;
}

int cpl_journal_reconcile_absent_after_crash(cpl_journal **inout_j,
    int parent_dirfd, const char *journal_name, int workdir_parent_dirfd,
    const char *workdir_name, const struct cpl_delete_authority *authority,
    uint64_t deadline_ns, struct cpl_delete_receipt *receipt) {
    struct cpl_certified_head certified;
    cpl_journal *journal;
    uint64_t deadline = effective_deadline(deadline_ns);
    int status;
    bool head_locked = false;

    if (receipt == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(receipt, 0, sizeof(*receipt));
    if (inout_j == NULL || *inout_j == NULL ||
        validate_component(journal_name) != CPL_OK) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    journal = *inout_j;
    status = ensure_owner(journal);
    if (status != CPL_OK) {
        return status;
    }
    status = validate_parent_identity(journal, parent_dirfd);
    if (status != CPL_OK) {
        return status;
    }
    status = lock_action(journal, deadline);
    if (status != CPL_OK) {
        return status;
    }
    status = validate_authority(journal, authority, deadline, &certified);
    if (status != CPL_OK) {
        goto done;
    }
    status = lock_certified_head(journal, &certified, deadline);
    if (status != CPL_OK) {
        goto done;
    }
    head_locked = true;
    status = require_workdir_absent(workdir_parent_dirfd, workdir_name);
    if (status != CPL_OK) {
        goto done;
    }
    status = validate_parent_identity(journal, workdir_parent_dirfd);
    if (status != CPL_OK) {
        goto done;
    }
    status = validate_open_journal_identity(journal, parent_dirfd,
        journal_name, true);
    if (status != CPL_OK) {
        goto done;
    }
    status = validate_parent_identity(journal, parent_dirfd);
    if (status != CPL_OK || fsync(parent_dirfd) < 0) {
        if (status == CPL_OK) {
            status = CPL_ERR_SYSTEM;
        }
        goto done;
    }
    status = CPL_OK;

done:
    if (head_locked) {
        unlock_certified_head(journal);
    }
    unlock_action(journal);
    if (status == CPL_OK) {
        successful_delete_close(inout_j, receipt);
    }
    return status;
}

void cpl_journal_close(cpl_journal *journal) {
    if (journal == NULL) {
        return;
    }
    if (journal->fd >= 0) {
        (void)close(journal->fd);
    }
    if (journal->append_lock_fd >= 0) {
        (void)close(journal->append_lock_fd);
    }
    if (journal->action_lock_fd >= 0) {
        (void)close(journal->action_lock_fd);
    }
    (void)pthread_mutex_destroy(&journal->append_mutex);
    (void)pthread_mutex_destroy(&journal->action_mutex);
    (void)memset(journal, 0, sizeof(*journal));
    free(journal);
}

#ifdef CPL_ENABLE_FAULT_INJECTION
int cpl_fault_hold_append_lock(cpl_journal *journal, int notify_fd, int wait_fd,
    uint64_t deadline_ns) {
    uint64_t deadline = effective_deadline(deadline_ns);
    int status = ensure_owner(journal);

    if (status != CPL_OK || notify_fd < 0 || wait_fd < 0) {
        return status == CPL_OK ? CPL_ERR_INVALID_ARGUMENT : status;
    }
    status = take_mutex(&journal->append_mutex, deadline);
    if (status != CPL_OK) {
        return status;
    }
    status = take_flock(journal->append_lock_fd, deadline);
    if (status != CPL_OK) {
        (void)pthread_mutex_unlock(&journal->append_mutex);
        return status;
    }
    status = checked_byte_write(notify_fd);
    if (status == CPL_OK) {
        status = checked_byte_read(wait_fd);
    }
    (void)release_flock(journal->append_lock_fd);
    (void)pthread_mutex_unlock(&journal->append_mutex);
    return status;
}
#endif
