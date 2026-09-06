#include "lifecycle.h"

#include <CommonCrypto/CommonDigest.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <libproc.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/proc.h>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define CPL_FORMAT_VERSION 1U
#define CPL_HEADER_SIZE 108U
#define CPL_MAGIC_SIZE 8U
#define CPL_MAX_NAME 180U
#define CPL_LOCK_RETRY_NS 500000ULL
#define CPL_DEFAULT_DEADLINE_NS 1000000000ULL
#define CPL_BOOTSTRAP_RECORD_SIZE 356U
#define CPL_BOOTSTRAP_PAYLOAD_OFFSET 96U

static const uint8_t CPL_MAGIC[CPL_MAGIC_SIZE] = {
    'C', 'P', 'L', 'J', 'R', 'N', '0', '1'
};
static const uint8_t CPL_BOOTSTRAP_MAGIC[CPL_MAGIC_SIZE] = {
    'C', 'P', 'L', 'B', 'O', 'O', 'T', '1'
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
    dev_t workdir_parent_dev;
    ino_t workdir_parent_ino;
    char workdir_name[CPL_WORKDIR_NAME_SIZE];
    uint8_t create_capability[CPL_HASH_SIZE];
    uint8_t workdir_capability[CPL_HASH_SIZE];
    uint8_t action_capability[CPL_HASH_SIZE];
    uint8_t reap_capability[CPL_HASH_SIZE];
    uint8_t reap_head_hash[CPL_HASH_SIZE];
    uint64_t reaped_generation;
    uint64_t reaped_authority_epoch;
    struct cpl_process_identity reaped_identity;
    struct cpl_append_result action_completion_result;
    uint64_t action_initial_completed_steps;
    uint64_t action_completed_steps;
    uint64_t action_effect_completed_steps;
    uint32_t authorized_record_kind;
    pthread_t authorized_thread;
    pthread_t action_owner_thread;
    bool action_held;
    bool action_executed;
    bool action_effect_started;
    bool action_completion_pending;
    bool reap_proof_valid;
    bool fork_invalid;
    bool unhealthy;
#ifdef CPL_ENABLE_FAULT_INJECTION
    uint32_t pause_point;
    int pause_notify_fd;
    int pause_wait_fd;
    uint32_t fail_batch_after_step;
    uint32_t fail_batch_after_effect;
    bool fail_workdir_parent_fsync;
#endif
    struct cpl_journal *registry_next;
};

_Static_assert(sizeof(struct cpl_process_identity) ==
    CPL_ABI_PROCESS_IDENTITY_SIZE,
    "cpl_process_identity ABI layout changed");
_Static_assert(sizeof(struct cpl_batch_descriptor) ==
    CPL_ABI_BATCH_DESCRIPTOR_SIZE,
    "cpl_batch_descriptor ABI layout changed");
_Static_assert(sizeof(struct cpl_state) == CPL_ABI_STATE_SIZE,
    "cpl_state ABI layout changed");
_Static_assert(sizeof(struct cpl_record) == CPL_ABI_RECORD_SIZE,
    "cpl_record ABI layout changed");
_Static_assert(sizeof(struct cpl_chain) == CPL_ABI_CHAIN_SIZE,
    "cpl_chain ABI layout changed");
_Static_assert(sizeof(struct cpl_certified_head) == CPL_ABI_CERTIFIED_HEAD_SIZE,
    "cpl_certified_head ABI layout changed");
_Static_assert(sizeof(struct cpl_supervisor_config) ==
    CPL_ABI_SUPERVISOR_CONFIG_SIZE,
    "cpl_supervisor_config ABI layout changed");
_Static_assert(sizeof(struct cpl_append_result) == CPL_ABI_APPEND_RESULT_SIZE,
    "cpl_append_result ABI layout changed");
_Static_assert(sizeof(struct cpl_create_receipt) == CPL_ABI_CREATE_RECEIPT_SIZE,
    "cpl_create_receipt ABI layout changed");
_Static_assert(sizeof(struct cpl_workdir_receipt) ==
    CPL_ABI_WORKDIR_RECEIPT_SIZE,
    "cpl_workdir_receipt ABI layout changed");
_Static_assert(sizeof(struct cpl_delete_authority) ==
    CPL_ABI_DELETE_AUTHORITY_SIZE,
    "cpl_delete_authority ABI layout changed");
_Static_assert(sizeof(struct cpl_delete_receipt) == CPL_ABI_DELETE_RECEIPT_SIZE,
    "cpl_delete_receipt ABI layout changed");
_Static_assert(sizeof(struct cpl_action_token) == CPL_ABI_ACTION_TOKEN_SIZE,
    "cpl_action_token ABI layout changed");
_Static_assert(sizeof(struct cpl_reap_proof) == CPL_ABI_REAP_PROOF_SIZE,
    "cpl_reap_proof ABI layout changed");
_Static_assert(sizeof(struct cpl_cleanup_evidence) ==
    CPL_ABI_CLEANUP_EVIDENCE_SIZE,
    "cpl_cleanup_evidence ABI layout changed");
_Static_assert(sizeof(struct cpl_cleanup_ack) == CPL_ABI_CLEANUP_ACK_SIZE,
    "cpl_cleanup_ack ABI layout changed");
_Static_assert(sizeof(struct cpl_control_frame) == CPL_ABI_CONTROL_FRAME_SIZE,
    "cpl_control_frame ABI layout changed");
_Static_assert(CPL_HEADER_SIZE + sizeof(struct cpl_record) ==
    CPL_PHYSICAL_RECORD_SIZE, "physical record size changed");
_Static_assert(CPL_MAX_AUTHORITY_EPOCH == 2U,
    "recovery tail permits exactly one authority replacement");
_Static_assert(CPL_MAX_RECOVERY_CLEANUP_BATCHES == 4U,
    "one recovery cleanup batch per fixed cleanup step");
_Static_assert(CPL_RECOVERY_RECORD_COUNT == 14U,
    "recovery tail derivation changed");

static pthread_mutex_t registry_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_once_t atfork_once = PTHREAD_ONCE_INIT;
static cpl_journal *registry_head = NULL;
static int atfork_status = CPL_ERR_SYSTEM;

static int ensure_owner(cpl_journal *journal);
static int take_mutex(pthread_mutex_t *mutex, uint64_t deadline);
static int take_flock(int fd, uint64_t deadline);
static int release_flock(int fd);

#ifdef CPL_ENABLE_FAULT_INJECTION
#define CPL_RECEIPT_REGISTRY_CAPACITY 64U
static pthread_mutex_t receipt_mutex = PTHREAD_MUTEX_INITIALIZER;
static uint8_t delete_receipts[CPL_RECEIPT_REGISTRY_CAPACITY][CPL_HASH_SIZE];
static bool delete_receipt_used[CPL_RECEIPT_REGISTRY_CAPACITY];
static bool force_atfork_registration_failure = false;
#endif

static int validate_parent_identity(cpl_journal *journal, int parent_dirfd);
static int validate_workdir_parent_identity(cpl_journal *journal,
    int parent_dirfd);

static void atfork_prepare(void) {
    (void)pthread_mutex_lock(&registry_mutex);
}

static void atfork_parent(void) {
    (void)pthread_mutex_unlock(&registry_mutex);
}

static void atfork_child(void) {
    cpl_journal *journal = registry_head;

    while (journal != NULL) {
        if (journal->fd >= 0) {
            (void)close(journal->fd);
            journal->fd = -1;
        }
        if (journal->append_lock_fd >= 0) {
            (void)close(journal->append_lock_fd);
            journal->append_lock_fd = -1;
        }
        if (journal->action_lock_fd >= 0) {
            (void)close(journal->action_lock_fd);
            journal->action_lock_fd = -1;
        }
        journal->fork_invalid = true;
        journal = journal->registry_next;
    }
    registry_head = NULL;
    (void)pthread_mutex_unlock(&registry_mutex);
}

static void install_atfork(void) {
    atfork_status = pthread_atfork(atfork_prepare, atfork_parent,
        atfork_child) == 0 ? CPL_OK : CPL_ERR_SYSTEM;
}

static int register_handle(cpl_journal *journal) {
    if (pthread_once(&atfork_once, install_atfork) != 0 ||
        atfork_status != CPL_OK
#ifdef CPL_ENABLE_FAULT_INJECTION
        || force_atfork_registration_failure
#endif
        ||
        pthread_mutex_lock(&registry_mutex) != 0) {
        return CPL_ERR_SYSTEM;
    }
    journal->registry_next = registry_head;
    registry_head = journal;
    (void)pthread_mutex_unlock(&registry_mutex);
    return CPL_OK;
}

static void unregister_handle(cpl_journal *journal) {
    cpl_journal **cursor;

    if (journal->fork_invalid || pthread_mutex_lock(&registry_mutex) != 0) {
        return;
    }
    cursor = &registry_head;
    while (*cursor != NULL) {
        if (*cursor == journal) {
            *cursor = journal->registry_next;
            break;
        }
        cursor = &(*cursor)->registry_next;
    }
    (void)pthread_mutex_unlock(&registry_mutex);
}

static uint64_t monotonic_ns(void) {
    struct timespec now;

    if (clock_gettime(CPL_DEADLINE_CLOCK, &now) < 0) {
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
        if (monotonic_ns() >= deadline_ns) {
            return CPL_ERR_LOCK_TIMEOUT;
        }
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
        if (monotonic_ns() >= deadline_ns) {
            return CPL_ERR_LOCK_TIMEOUT;
        }
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

static void random_capability(uint8_t out[CPL_HASH_SIZE]) {
    arc4random_buf(out, CPL_HASH_SIZE);
}

static bool same_capability(const uint8_t left[CPL_HASH_SIZE],
    const uint8_t right[CPL_HASH_SIZE]) {
    return !is_zero(left, CPL_HASH_SIZE) &&
        memcmp(left, right, CPL_HASH_SIZE) == 0;
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

static bool valid_control_type(uint16_t type) {
    return type >= CPL_CONTROL_SUPERVISOR_IDENTITY &&
        type <= CPL_CONTROL_CLEANUP_ACK;
}

int cpl_control_frame_encode(uint16_t type,
    const uint8_t allocation_nonce[CPL_HASH_SIZE], const uint8_t *payload,
    uint32_t payload_length, uint8_t *out, uint32_t out_capacity,
    uint32_t *out_length) {
    uint32_t wire_length;
    uint32_t checksum;

    if (out_length != NULL) {
        *out_length = 0U;
    }
    if (!valid_control_type(type) || allocation_nonce == NULL ||
        is_zero(allocation_nonce, CPL_HASH_SIZE) || out == NULL ||
        out_length == NULL || payload_length > CPL_CONTROL_MAX_PAYLOAD ||
        (payload_length > 0U && payload == NULL)) {
        return payload_length > CPL_CONTROL_MAX_PAYLOAD ?
            CPL_ERR_CONTROL_PAYLOAD : CPL_ERR_INVALID_ARGUMENT;
    }
    wire_length = CPL_CONTROL_WIRE_HEADER_SIZE + payload_length +
        CPL_CONTROL_WIRE_CHECKSUM_SIZE;
    if (out_capacity < wire_length) {
        return CPL_ERR_CONTROL_PAYLOAD;
    }
    (void)memset(out, 0, wire_length);
    put_u32(out, CPL_CONTROL_MAGIC);
    put_u16(out + 4U, CPL_CONTROL_VERSION);
    put_u16(out + 6U, type);
    put_u32(out + 8U, payload_length);
    (void)memcpy(out + 12U, allocation_nonce, CPL_HASH_SIZE);
    if (payload_length > 0U) {
        (void)memcpy(out + CPL_CONTROL_WIRE_HEADER_SIZE, payload,
            payload_length);
    }
    checksum = crc32c(out, wire_length - CPL_CONTROL_WIRE_CHECKSUM_SIZE);
    put_u32(out + wire_length - CPL_CONTROL_WIRE_CHECKSUM_SIZE, checksum);
    *out_length = wire_length;
    return CPL_OK;
}

int cpl_control_frame_decode(const uint8_t *wire, uint32_t wire_length,
    const uint8_t expected_nonce[CPL_HASH_SIZE],
    struct cpl_control_frame *out) {
    uint32_t payload_length;
    uint32_t expected_length;
    uint32_t checksum;
    uint32_t stored_checksum;
    uint16_t type;

    if (out == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(out, 0, sizeof(*out));
    if (wire == NULL || expected_nonce == NULL ||
        wire_length < CPL_CONTROL_WIRE_HEADER_SIZE +
            CPL_CONTROL_WIRE_CHECKSUM_SIZE) {
        return CPL_ERR_CONTROL_FRAME;
    }
    payload_length = get_u32(wire + 8U);
    if (payload_length > CPL_CONTROL_MAX_PAYLOAD) {
        return CPL_ERR_CONTROL_PAYLOAD;
    }
    expected_length = CPL_CONTROL_WIRE_HEADER_SIZE + payload_length +
        CPL_CONTROL_WIRE_CHECKSUM_SIZE;
    if (wire_length != expected_length ||
        get_u32(wire) != CPL_CONTROL_MAGIC ||
        get_u16(wire + 4U) != CPL_CONTROL_VERSION) {
        return CPL_ERR_CONTROL_FRAME;
    }
    type = get_u16(wire + 6U);
    if (!valid_control_type(type) ||
        memcmp(wire + 12U, expected_nonce, CPL_HASH_SIZE) != 0) {
        return CPL_ERR_CONTROL_FRAME;
    }
    stored_checksum = get_u32(
        wire + wire_length - CPL_CONTROL_WIRE_CHECKSUM_SIZE);
    checksum = crc32c(wire, wire_length - CPL_CONTROL_WIRE_CHECKSUM_SIZE);
    if (stored_checksum != checksum) {
        return CPL_ERR_CONTROL_FRAME;
    }
    out->magic = CPL_CONTROL_MAGIC;
    out->version = CPL_CONTROL_VERSION;
    out->type = type;
    out->payload_length = payload_length;
    (void)memcpy(out->allocation_nonce, expected_nonce, CPL_HASH_SIZE);
    if (payload_length > 0U) {
        (void)memcpy(out->payload, wire + CPL_CONTROL_WIRE_HEADER_SIZE,
            payload_length);
    }
    out->checksum = stored_checksum;
    return CPL_OK;
}

static int control_wait(int fd, short events, uint64_t deadline) {
    struct pollfd descriptor;

    for (;;) {
        uint64_t now = monotonic_ns();
        uint64_t remaining;
        uint64_t milliseconds;
        int timeout;
        int result;

        if (now == 0U || now >= deadline) {
            return CPL_ERR_CERTIFY_TIMEOUT;
        }
        remaining = deadline - now;
        milliseconds = (remaining + 999999U) / 1000000U;
        timeout = milliseconds > (uint64_t)INT_MAX ? INT_MAX :
            (int)milliseconds;
        descriptor.fd = fd;
        descriptor.events = events;
        descriptor.revents = 0;
        result = poll(&descriptor, 1U, timeout);
        if (result < 0 && errno == EINTR) {
            continue;
        }
        if (result < 0) {
            return CPL_ERR_SYSTEM;
        }
        if (result == 0) {
            return CPL_ERR_CERTIFY_TIMEOUT;
        }
        if ((descriptor.revents & events) != 0) {
            return CPL_OK;
        }
        return CPL_ERR_CONTROL_FRAME;
    }
}

static int control_transfer(int fd, uint8_t *bytes, uint32_t length,
    uint64_t deadline, bool writing) {
    uint32_t offset = 0U;

    if (fd < 0 || bytes == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    while (offset < length) {
        ssize_t result;
        int status = control_wait(fd, writing ? POLLOUT : POLLIN, deadline);

        if (status != CPL_OK) {
            return status;
        }
        if (writing) {
            result = write(fd, bytes + offset, (size_t)(length - offset));
        } else {
            result = read(fd, bytes + offset, (size_t)(length - offset));
        }
        if (result < 0 && (errno == EINTR || errno == EAGAIN)) {
            continue;
        }
        if (result <= 0) {
            return CPL_ERR_CONTROL_FRAME;
        }
        offset += (uint32_t)result;
    }
    return CPL_OK;
}

int cpl_control_frame_write(int fd, uint16_t type,
    const uint8_t allocation_nonce[CPL_HASH_SIZE], const uint8_t *payload,
    uint32_t payload_length, uint64_t deadline_ns) {
    uint8_t wire[CPL_CONTROL_MAX_WIRE_SIZE];
    uint32_t wire_length = 0U;
    uint64_t deadline = effective_deadline(deadline_ns);
    int status = cpl_control_frame_encode(type, allocation_nonce, payload,
        payload_length, wire, sizeof(wire), &wire_length);

    if (status != CPL_OK) {
        return status;
    }
    return control_transfer(fd, wire, wire_length, deadline, true);
}

int cpl_control_frame_read(int fd,
    const uint8_t expected_nonce[CPL_HASH_SIZE], uint64_t deadline_ns,
    struct cpl_control_frame *out) {
    uint8_t wire[CPL_CONTROL_MAX_WIRE_SIZE];
    uint32_t payload_length;
    uint32_t wire_length;
    uint64_t deadline = effective_deadline(deadline_ns);
    int status;

    if (out == NULL || expected_nonce == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(out, 0, sizeof(*out));
    status = control_transfer(fd, wire, CPL_CONTROL_WIRE_HEADER_SIZE,
        deadline, false);
    if (status != CPL_OK) {
        return status;
    }
    payload_length = get_u32(wire + 8U);
    if (payload_length > CPL_CONTROL_MAX_PAYLOAD) {
        return CPL_ERR_CONTROL_PAYLOAD;
    }
    wire_length = CPL_CONTROL_WIRE_HEADER_SIZE + payload_length +
        CPL_CONTROL_WIRE_CHECKSUM_SIZE;
    status = control_transfer(fd, wire + CPL_CONTROL_WIRE_HEADER_SIZE,
        payload_length + CPL_CONTROL_WIRE_CHECKSUM_SIZE, deadline, false);
    if (status != CPL_OK) {
        return status;
    }
    return cpl_control_frame_decode(wire, wire_length, expected_nonce, out);
}

int cpl_control_phase_accept(uint32_t *inout_phase, uint16_t type,
    bool durable_head_certified) {
    uint32_t expected;

    if (inout_phase == NULL || *inout_phase > CPL_CONTROL_PHASE_CLEANUP_ACK ||
        !valid_control_type(type)) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    if (type == CPL_CONTROL_ERROR) {
        if (*inout_phase == CPL_CONTROL_PHASE_ERROR) {
            return CPL_ERR_CONTROL_PHASE;
        }
        *inout_phase = CPL_CONTROL_PHASE_ERROR;
        return CPL_OK;
    }
    if (type == CPL_CONTROL_CLEANUP_ACK) {
        if (*inout_phase != CPL_CONTROL_PHASE_CLEANUP_REQUEST ||
            !durable_head_certified) {
            return CPL_ERR_CONTROL_PHASE;
        }
        *inout_phase = CPL_CONTROL_PHASE_CLEANUP_ACK;
        return CPL_OK;
    }
    expected = *inout_phase + 1U;
    if (((uint32_t)type != expected &&
         !(type == CPL_CONTROL_SELF_TERM_REQUEST &&
           *inout_phase == CPL_CONTROL_PHASE_CLI_RUNNING) &&
         !(type == CPL_CONTROL_CLEANUP_RESULT &&
           *inout_phase == CPL_CONTROL_PHASE_CLEANUP_ACK)) ||
        ((type == CPL_CONTROL_IDENTITY_ACK ||
          type == CPL_CONTROL_ANCHOR_ACK ||
          type == CPL_CONTROL_ARMED_ACK) && !durable_head_certified)) {
        return CPL_ERR_CONTROL_PHASE;
    }
    *inout_phase = (uint32_t)type;
    return CPL_OK;
}

struct bootstrap_chain {
    uint64_t sequence;
    uint64_t physical_eof;
    uint16_t type;
    uint32_t payload_length;
    uint8_t hash[CPL_HASH_SIZE];
    uint8_t payload[CPL_BOOTSTRAP_MAX_PAYLOAD];
    bool present;
};

static bool bootstrap_transition(uint16_t current, bool present,
    uint16_t next) {
    if (!present) {
        return next == CPL_CONTROL_SUPERVISOR_IDENTITY;
    }
    if (next == CPL_CONTROL_ERROR && current != CPL_CONTROL_ERROR) {
        return true;
    }
    if (current == CPL_CONTROL_SUPERVISOR_IDENTITY) {
        return next == CPL_CONTROL_ANCHOR_IDENTITY;
    }
    if (current == CPL_CONTROL_ANCHOR_IDENTITY) {
        return next == CPL_CONTROL_CLI_ARMED;
    }
    if (current == CPL_CONTROL_CLI_ARMED) {
        return next == CPL_CONTROL_CLI_RUNNING;
    }
    if (current == CPL_CONTROL_CLI_RUNNING) {
        return next == CPL_CONTROL_CLEANUP_REQUEST ||
            next == CPL_CONTROL_SELF_TERM_REQUEST;
    }
    if (current == CPL_CONTROL_CLEANUP_REQUEST) {
        return next == CPL_CONTROL_CLEANUP_RESULT;
    }
    return false;
}

static bool decode_bootstrap_record(cpl_journal *journal,
    const uint8_t *record, uint64_t expected_sequence,
    const uint8_t expected_parent[CPL_HASH_SIZE], uint16_t current_type,
    bool present, struct bootstrap_chain *out) {
    uint8_t candidate[CPL_BOOTSTRAP_RECORD_SIZE];
    uint32_t payload_length;
    uint32_t stored_checksum;
    uint16_t type;

    if (memcmp(record, CPL_BOOTSTRAP_MAGIC, CPL_MAGIC_SIZE) != 0 ||
        get_u16(record + 8U) != CPL_CONTROL_VERSION ||
        get_u16(record + 10U) != CPL_BOOTSTRAP_PAYLOAD_OFFSET ||
        get_u32(record + 12U) != CPL_BOOTSTRAP_RECORD_SIZE ||
        memcmp(record + 16U, journal->nonce, CPL_HASH_SIZE) != 0 ||
        get_u64(record + 48U) != expected_sequence ||
        memcmp(record + 56U, expected_parent, CPL_HASH_SIZE) != 0) {
        return false;
    }
    type = get_u16(record + 88U);
    payload_length = get_u32(record + 92U);
    if (payload_length > CPL_BOOTSTRAP_MAX_PAYLOAD ||
        !bootstrap_transition(current_type, present, type)) {
        return false;
    }
    (void)memcpy(candidate, record, sizeof(candidate));
    stored_checksum = get_u32(candidate + CPL_BOOTSTRAP_RECORD_SIZE - 4U);
    (void)memset(candidate + CPL_BOOTSTRAP_RECORD_SIZE - 4U, 0, 4U);
    if (crc32c(candidate, sizeof(candidate)) != stored_checksum) {
        return false;
    }
    out->sequence = expected_sequence;
    out->type = type;
    out->payload_length = payload_length;
    (void)memcpy(out->payload, record + CPL_BOOTSTRAP_PAYLOAD_OFFSET,
        payload_length);
    sha256(record, CPL_BOOTSTRAP_RECORD_SIZE, out->hash);
    out->present = true;
    return true;
}

static int scan_bootstrap(cpl_journal *journal, struct bootstrap_chain *out) {
    struct stat metadata;
    uint8_t *bytes;
    uint64_t offset = 0U;
    uint8_t zero_hash[CPL_HASH_SIZE] = {0};
    ssize_t got;

    (void)memset(out, 0, sizeof(*out));
    if (fstat(journal->fd, &metadata) < 0 || metadata.st_size < 0 ||
        (uint64_t)metadata.st_size > journal->hard_limit) {
        return CPL_ERR_CORRUPT;
    }
    out->physical_eof = (uint64_t)metadata.st_size;
    if (out->physical_eof == 0U) {
        return CPL_OK;
    }
    bytes = malloc((size_t)out->physical_eof);
    if (bytes == NULL) {
        return CPL_ERR_SYSTEM;
    }
    got = pread(journal->fd, bytes, (size_t)out->physical_eof, 0);
    if (got < 0 || (uint64_t)got != out->physical_eof) {
        free(bytes);
        return CPL_ERR_SYSTEM;
    }
    while (offset + CPL_BOOTSTRAP_RECORD_SIZE <= out->physical_eof) {
        if (memcmp(bytes + offset, CPL_BOOTSTRAP_MAGIC,
                CPL_MAGIC_SIZE) == 0) {
            struct bootstrap_chain candidate = *out;
            const uint8_t *parent = out->present ? out->hash : zero_hash;

            if (decode_bootstrap_record(journal, bytes + offset,
                    out->sequence + 1U, parent, out->type, out->present,
                    &candidate)) {
                candidate.physical_eof = out->physical_eof;
                *out = candidate;
                offset += CPL_BOOTSTRAP_RECORD_SIZE;
                continue;
            }
        }
        ++offset;
    }
    free(bytes);
    return CPL_OK;
}

static bool valid_physical_bootstrap_record(cpl_journal *journal,
    const uint8_t *record, uint64_t remaining) {
    uint8_t candidate[CPL_BOOTSTRAP_RECORD_SIZE];
    uint32_t stored_checksum;

    if (remaining < CPL_BOOTSTRAP_RECORD_SIZE ||
        memcmp(record, CPL_BOOTSTRAP_MAGIC, CPL_MAGIC_SIZE) != 0 ||
        get_u16(record + 8U) != CPL_CONTROL_VERSION ||
        get_u16(record + 10U) != CPL_BOOTSTRAP_PAYLOAD_OFFSET ||
        get_u32(record + 12U) != CPL_BOOTSTRAP_RECORD_SIZE ||
        memcmp(record + 16U, journal->nonce, CPL_HASH_SIZE) != 0 ||
        get_u32(record + 92U) > CPL_BOOTSTRAP_MAX_PAYLOAD) {
        return false;
    }
    (void)memcpy(candidate, record, sizeof(candidate));
    stored_checksum = get_u32(candidate + CPL_BOOTSTRAP_RECORD_SIZE - 4U);
    (void)memset(candidate + CPL_BOOTSTRAP_RECORD_SIZE - 4U, 0, 4U);
    return crc32c(candidate, sizeof(candidate)) == stored_checksum;
}

static int bootstrap_certify_internal(cpl_journal *journal,
    uint16_t expected_type, const uint8_t *expected_payload,
    uint32_t expected_payload_length, uint64_t deadline,
    struct cpl_bootstrap_head *out) {
    uint32_t attempts = 0U;

    for (;;) {
        struct bootstrap_chain first;
        struct bootstrap_chain second;
        int status;

        ++attempts;
        status = take_mutex(&journal->append_mutex, deadline);
        if (status != CPL_OK) {
            return status;
        }
        status = take_flock(journal->append_lock_fd, deadline);
        if (status == CPL_OK) {
            status = scan_bootstrap(journal, &first);
            (void)release_flock(journal->append_lock_fd);
        }
        (void)pthread_mutex_unlock(&journal->append_mutex);
        if (status != CPL_OK || !first.present ||
            first.type != expected_type ||
            first.payload_length != expected_payload_length ||
            (expected_payload_length > 0U &&
             memcmp(first.payload, expected_payload,
                 expected_payload_length) != 0)) {
            return status == CPL_OK ? CPL_ERR_AUTHORITY : status;
        }
        if (fcntl(journal->fd, F_FULLFSYNC) < 0) {
            return CPL_ERR_SYSTEM;
        }
        status = take_mutex(&journal->append_mutex, deadline);
        if (status != CPL_OK) {
            return status;
        }
        status = take_flock(journal->append_lock_fd, deadline);
        if (status == CPL_OK) {
            status = scan_bootstrap(journal, &second);
            (void)release_flock(journal->append_lock_fd);
        }
        (void)pthread_mutex_unlock(&journal->append_mutex);
        if (status != CPL_OK) {
            return status;
        }
        if (first.physical_eof == second.physical_eof &&
            first.sequence == second.sequence &&
            memcmp(first.hash, second.hash, CPL_HASH_SIZE) == 0) {
            (void)memset(out, 0, sizeof(*out));
            out->sequence = second.sequence;
            out->physical_eof = second.physical_eof;
            out->type = second.type;
            out->payload_length = second.payload_length;
            out->attempts = attempts;
            out->present = true;
            (void)memcpy(out->hash, second.hash, CPL_HASH_SIZE);
            (void)memcpy(out->payload, second.payload,
                second.payload_length);
            return CPL_OK;
        }
        if (monotonic_ns() >= deadline) {
            return CPL_ERR_CERTIFY_TIMEOUT;
        }
    }
}

int cpl_journal_bootstrap_certify(cpl_journal *journal,
    uint16_t expected_type, const uint8_t *expected_payload,
    uint32_t expected_payload_length, uint64_t deadline_ns,
    struct cpl_bootstrap_head *out) {
    uint64_t deadline = effective_deadline(deadline_ns);
    int status;

    if (out == NULL || !valid_control_type(expected_type) ||
        expected_payload_length > CPL_BOOTSTRAP_MAX_PAYLOAD ||
        (expected_payload_length > 0U && expected_payload == NULL)) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    status = ensure_owner(journal);
    return status == CPL_OK ? bootstrap_certify_internal(journal,
        expected_type, expected_payload, expected_payload_length, deadline,
        out) : status;
}

int cpl_journal_bootstrap_append(cpl_journal *journal, uint16_t type,
    const uint8_t *payload, uint32_t payload_length, uint64_t deadline_ns,
    struct cpl_bootstrap_head *out) {
    struct bootstrap_chain chain;
    uint8_t record[CPL_BOOTSTRAP_RECORD_SIZE];
    uint8_t zero_hash[CPL_HASH_SIZE] = {0};
    const uint8_t *parent;
    struct stat metadata;
    uint64_t deadline = effective_deadline(deadline_ns);
    uint32_t checksum;
    ssize_t written;
    int status;

    if (out == NULL || payload_length > CPL_BOOTSTRAP_MAX_PAYLOAD ||
        (payload_length > 0U && payload == NULL) ||
        !valid_control_type(type)) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(out, 0, sizeof(*out));
    status = ensure_owner(journal);
    if (status != CPL_OK) {
        return status;
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
    status = scan_bootstrap(journal, &chain);
    if (status == CPL_OK && !bootstrap_transition(chain.type, chain.present,
            type)) {
        status = CPL_ERR_CONTROL_PHASE;
    }
    if (status == CPL_OK && (fstat(journal->fd, &metadata) < 0 ||
            metadata.st_size < 0 ||
            (uint64_t)metadata.st_size + CPL_BOOTSTRAP_RECORD_SIZE >
                journal->normal_limit)) {
        status = CPL_ERR_NORMAL_LIMIT;
    }
    if (status == CPL_OK) {
        (void)memset(record, 0, sizeof(record));
        (void)memcpy(record, CPL_BOOTSTRAP_MAGIC, CPL_MAGIC_SIZE);
        put_u16(record + 8U, CPL_CONTROL_VERSION);
        put_u16(record + 10U, CPL_BOOTSTRAP_PAYLOAD_OFFSET);
        put_u32(record + 12U, CPL_BOOTSTRAP_RECORD_SIZE);
        (void)memcpy(record + 16U, journal->nonce, CPL_HASH_SIZE);
        put_u64(record + 48U, chain.sequence + 1U);
        parent = chain.present ? chain.hash : zero_hash;
        (void)memcpy(record + 56U, parent, CPL_HASH_SIZE);
        put_u16(record + 88U, type);
        put_u32(record + 92U, payload_length);
        if (payload_length > 0U) {
            (void)memcpy(record + CPL_BOOTSTRAP_PAYLOAD_OFFSET, payload,
                payload_length);
        }
        checksum = crc32c(record, sizeof(record));
        put_u32(record + CPL_BOOTSTRAP_RECORD_SIZE - 4U, checksum);
        written = write(journal->fd, record, sizeof(record));
        if (written < 0 || (size_t)written != sizeof(record)) {
            journal->unhealthy = true;
            status = CPL_ERR_IO_SHORT;
        }
    }
    (void)release_flock(journal->append_lock_fd);
    (void)pthread_mutex_unlock(&journal->append_mutex);
    if (status != CPL_OK) {
        return status;
    }
    return bootstrap_certify_internal(journal, type, payload, payload_length,
        deadline, out);
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

static int validate_workdir_component(const char *name) {
    size_t length;

    if (name == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    length = strnlen(name, CPL_WORKDIR_NAME_SIZE);
    if (length == 0U || length >= CPL_WORKDIR_NAME_SIZE ||
        strchr(name, '/') != NULL || strcmp(name, ".") == 0 ||
        strcmp(name, "..") == 0) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    return CPL_OK;
}

static int derive_workdir_name(const char *journal_name,
    char out[CPL_WORKDIR_NAME_SIZE]) {
    static const char journal_suffix[] = ".journal";
    static const char workdir_suffix[] = ".workdir";
    size_t name_length;
    size_t stem_length;

    if (validate_component(journal_name) != CPL_OK) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    name_length = strlen(journal_name);
    if (name_length <= sizeof(journal_suffix) - 1U ||
        memcmp(journal_name + name_length - (sizeof(journal_suffix) - 1U),
            journal_suffix, sizeof(journal_suffix) - 1U) != 0) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    stem_length = name_length - (sizeof(journal_suffix) - 1U);
    if (stem_length + sizeof(workdir_suffix) > CPL_WORKDIR_NAME_SIZE) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memcpy(out, journal_name, stem_length);
    (void)memcpy(out + stem_length, workdir_suffix,
        sizeof(workdir_suffix));
    return CPL_OK;
}

static int validate_derived_workdir_name(const char *journal_name,
    const char *requested_workdir_name) {
    char derived[CPL_WORKDIR_NAME_SIZE] = {0};
    int status = derive_workdir_name(journal_name, derived);

    if (status != CPL_OK ||
        validate_workdir_component(requested_workdir_name) != CPL_OK ||
        strcmp(derived, requested_workdir_name) != 0) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    return CPL_OK;
}

static int hash_fd(int fd, uint8_t out[CPL_HASH_SIZE]) {
    struct stat metadata;
    uint8_t *content;
    ssize_t got;

    if (fstat(fd, &metadata) < 0 || metadata.st_size < 0 ||
        metadata.st_size > (off_t)(64U * 1024U * 1024U)) {
        return CPL_ERR_PROCESS_IDENTITY;
    }
    content = malloc(metadata.st_size == 0 ? 1U : (size_t)metadata.st_size);
    if (content == NULL) {
        return CPL_ERR_SYSTEM;
    }
    got = pread(fd, content, (size_t)metadata.st_size, 0);
    if (got < 0 || got != metadata.st_size) {
        free(content);
        return CPL_ERR_PROCESS_IDENTITY;
    }
    sha256(content, (size_t)metadata.st_size, out);
    free(content);
    return CPL_OK;
}

static bool complete_identity(const struct cpl_process_identity *identity) {
    return identity != NULL && identity->pid > 0 &&
        identity->start_ns > 0U &&
        identity->flags == CPL_COMPLETE_PROCESS_IDENTITY &&
        identity->pgid > 0 && identity->sid > 0 &&
        identity->executable_dev > 0U && identity->executable_ino > 0U &&
        !is_zero(identity->boot_id, CPL_HASH_SIZE) &&
        !is_zero(identity->executable_hash, CPL_HASH_SIZE);
}

#ifdef CPL_ENABLE_FAULT_INJECTION
#define CPL_FAULT_PROCESS_CAPACITY 32U
static pthread_mutex_t fault_process_mutex = PTHREAD_MUTEX_INITIALIZER;
static struct cpl_process_identity
    fault_processes[CPL_FAULT_PROCESS_CAPACITY];
static uint8_t fault_boot_id[CPL_HASH_SIZE];

static int fault_process_observe(int64_t pid,
    struct cpl_process_identity *out) {
    char executable_path[PROC_PIDPATHINFO_MAXSIZE];
    struct stat executable;
    uint32_t path_size = (uint32_t)sizeof(executable_path);
    size_t index;
    int executable_fd;
    int status;

    if (kill((pid_t)pid, 0) < 0) {
        return errno == ESRCH ? CPL_ERR_NOT_FOUND : CPL_ERR_PROCESS_IDENTITY;
    }
    if (pthread_mutex_lock(&fault_process_mutex) != 0) {
        return CPL_ERR_SYSTEM;
    }
    for (index = 0U; index < CPL_FAULT_PROCESS_CAPACITY; ++index) {
        if (fault_processes[index].pid == pid) {
            *out = fault_processes[index];
            (void)pthread_mutex_unlock(&fault_process_mutex);
            return CPL_OK;
        }
    }
    if (_NSGetExecutablePath(executable_path, &path_size) != 0) {
        (void)pthread_mutex_unlock(&fault_process_mutex);
        return CPL_ERR_PROCESS_IDENTITY;
    }
    executable_fd = open(executable_path, O_RDONLY | O_CLOEXEC);
    if (executable_fd < 0 || fstat(executable_fd, &executable) < 0 ||
        !S_ISREG(executable.st_mode)) {
        if (executable_fd >= 0) {
            (void)close(executable_fd);
        }
        (void)pthread_mutex_unlock(&fault_process_mutex);
        return CPL_ERR_PROCESS_IDENTITY;
    }
    (void)memset(out, 0, sizeof(*out));
    out->pid = pid;
    out->start_ns = monotonic_ns();
    out->uid = geteuid();
    out->pgid = getpgid((pid_t)pid);
    out->sid = getsid((pid_t)pid);
    out->executable_dev = (uint64_t)executable.st_dev;
    out->executable_ino = (uint64_t)executable.st_ino;
    if (is_zero(fault_boot_id, CPL_HASH_SIZE)) {
        uint64_t boot_seed = monotonic_ns();

        sha256((const uint8_t *)&boot_seed, sizeof(boot_seed), fault_boot_id);
    }
    (void)memcpy(out->boot_id, fault_boot_id, CPL_HASH_SIZE);
    status = hash_fd(executable_fd, out->executable_hash);
    (void)close(executable_fd);
    if (status != CPL_OK || out->pgid <= 0 || out->sid <= 0) {
        (void)memset(out, 0, sizeof(*out));
        (void)pthread_mutex_unlock(&fault_process_mutex);
        return CPL_ERR_PROCESS_IDENTITY;
    }
    out->flags = CPL_COMPLETE_PROCESS_IDENTITY;
    for (index = 0U; index < CPL_FAULT_PROCESS_CAPACITY; ++index) {
        if (fault_processes[index].pid == 0) {
            fault_processes[index] = *out;
            break;
        }
    }
    (void)pthread_mutex_unlock(&fault_process_mutex);
    return index < CPL_FAULT_PROCESS_CAPACITY ? CPL_OK : CPL_ERR_SYSTEM;
}
#endif

int cpl_process_observe(int64_t pid, struct cpl_process_identity *out) {
    struct proc_bsdinfo process;
    struct timeval boot;
    struct stat executable;
    char path[PROC_PIDPATHINFO_MAXSIZE];
    size_t boot_size = sizeof(boot);
    int executable_fd;
    int status;

    if (out == NULL || pid <= 0 || pid > INT_MAX) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(out, 0, sizeof(*out));
    if (proc_pidinfo((int)pid, PROC_PIDTBSDINFO, 0U, &process,
            (int)sizeof(process)) != (int)sizeof(process) ||
        process.pbi_pid != (uint32_t)pid) {
#ifdef CPL_ENABLE_FAULT_INJECTION
        if (errno == EPERM) {
            return fault_process_observe(pid, out);
        }
#endif
        return errno == ESRCH ? CPL_ERR_NOT_FOUND : CPL_ERR_PROCESS_IDENTITY;
    }
    if (sysctlbyname("kern.boottime", &boot, &boot_size, NULL, 0U) < 0 ||
        boot_size != sizeof(boot) || boot.tv_sec <= 0) {
#ifdef CPL_ENABLE_FAULT_INJECTION
        if (errno == EPERM) {
            return fault_process_observe(pid, out);
        }
#endif
        return CPL_ERR_PROCESS_IDENTITY;
    }
    (void)memset(path, 0, sizeof(path));
    if (proc_pidpath((int)pid, path, (uint32_t)sizeof(path)) <= 0) {
#ifdef CPL_ENABLE_FAULT_INJECTION
        if (errno == EPERM) {
            return fault_process_observe(pid, out);
        }
#endif
        return CPL_ERR_PROCESS_IDENTITY;
    }
    executable_fd = open(path, O_RDONLY | O_CLOEXEC);
    if (executable_fd < 0 || fstat(executable_fd, &executable) < 0 ||
        !S_ISREG(executable.st_mode)) {
        if (executable_fd >= 0) {
            (void)close(executable_fd);
        }
        return CPL_ERR_PROCESS_IDENTITY;
    }
    out->pid = pid;
    out->start_ns = process.pbi_start_tvsec * 1000000000ULL +
        process.pbi_start_tvusec * 1000ULL;
    out->uid = process.pbi_uid;
    out->pgid = (int32_t)process.pbi_pgid;
    out->sid = (int32_t)getsid((pid_t)pid);
    out->executable_dev = (uint64_t)executable.st_dev;
    out->executable_ino = (uint64_t)executable.st_ino;
    sha256((const uint8_t *)&boot, sizeof(boot), out->boot_id);
    status = hash_fd(executable_fd, out->executable_hash);
    (void)close(executable_fd);
    if (status != CPL_OK || out->sid <= 0) {
        (void)memset(out, 0, sizeof(*out));
        return CPL_ERR_PROCESS_IDENTITY;
    }
    out->flags = CPL_COMPLETE_PROCESS_IDENTITY;
    return complete_identity(out) ? CPL_OK : CPL_ERR_PROCESS_IDENTITY;
}

static void identity_from_state(const struct cpl_state *state,
    struct cpl_process_identity *identity) {
    (void)memset(identity, 0, sizeof(*identity));
    identity->pid = state->process_pid;
    identity->start_ns = state->process_start_ns;
    identity->uid = state->process_uid;
    identity->pgid = state->process_pgid;
    identity->sid = state->process_sid;
    identity->flags = state->process_identity_flags;
    identity->executable_dev = state->executable_dev;
    identity->executable_ino = state->executable_ino;
    (void)memcpy(identity->boot_id, state->boot_id, CPL_HASH_SIZE);
    (void)memcpy(identity->executable_hash, state->executable_hash,
        CPL_HASH_SIZE);
}

static void identity_to_record(const struct cpl_process_identity *identity,
    struct cpl_record *record) {
    record->process_pid = identity->pid;
    record->process_start_ns = identity->start_ns;
    record->process_uid = identity->uid;
    record->process_pgid = identity->pgid;
    record->process_sid = identity->sid;
    record->process_identity_flags = identity->flags;
    record->executable_dev = identity->executable_dev;
    record->executable_ino = identity->executable_ino;
    (void)memcpy(record->boot_id, identity->boot_id, CPL_HASH_SIZE);
    (void)memcpy(record->executable_hash, identity->executable_hash,
        CPL_HASH_SIZE);
}

static bool same_process_identity(const struct cpl_process_identity *left,
    const struct cpl_process_identity *right) {
    return left->pid == right->pid && left->start_ns == right->start_ns &&
        left->uid == right->uid && left->pgid == right->pgid &&
        left->sid == right->sid && left->flags == right->flags &&
        left->executable_dev == right->executable_dev &&
        left->executable_ino == right->executable_ino &&
        memcmp(left->boot_id, right->boot_id, CPL_HASH_SIZE) == 0 &&
        memcmp(left->executable_hash, right->executable_hash,
            CPL_HASH_SIZE) == 0;
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

static int validate_limits(uint64_t normal_limit, uint64_t hard_limit) {
    if (normal_limit < CPL_PHYSICAL_RECORD_SIZE ||
        normal_limit >= hard_limit || hard_limit > (uint64_t)INT64_MAX ||
        hard_limit - normal_limit != CPL_RECOVERY_BYTES) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    return CPL_OK;
}

static int initialize_handle(int fd, int parent_dirfd, const char *journal_name,
    int workdir_parent_dirfd, const char *workdir_name,
    const uint8_t nonce[CPL_HASH_SIZE], uint64_t normal_limit,
    uint64_t hard_limit, cpl_journal **out) {
    struct stat parent_stat;
    struct stat workdir_parent_stat;
    struct stat journal_stat;
    cpl_journal *journal;
    int append_fd = -1;
    int action_fd = -1;
    int status;

    status = validate_parent(parent_dirfd, &parent_stat);
    if (status != CPL_OK) {
        return status;
    }
    status = validate_parent(workdir_parent_dirfd, &workdir_parent_stat);
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
    journal->workdir_parent_dev = workdir_parent_stat.st_dev;
    journal->workdir_parent_ino = workdir_parent_stat.st_ino;
#ifdef CPL_ENABLE_FAULT_INJECTION
    journal->pause_notify_fd = -1;
    journal->pause_wait_fd = -1;
#endif
    (void)memcpy(journal->nonce, nonce, CPL_HASH_SIZE);
    (void)memcpy(journal->journal_name, journal_name, strlen(journal_name) + 1U);
    (void)memcpy(journal->workdir_name, workdir_name,
        strlen(workdir_name) + 1U);
    random_capability(journal->create_capability);
    random_capability(journal->workdir_capability);
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
    status = register_handle(journal);
    if (status != CPL_OK) {
        (void)pthread_mutex_destroy(&journal->action_mutex);
        (void)pthread_mutex_destroy(&journal->append_mutex);
        free(journal);
        (void)close(action_fd);
        (void)close(append_fd);
        return status;
    }
    *out = journal;
    return CPL_OK;
}

static int ensure_owner(cpl_journal *journal) {
    if (journal == NULL) {
        return CPL_ERR_CLOSED;
    }
    if (journal->fork_invalid || journal->owner_pid != getpid()) {
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
    out->descriptor_count = record->descriptor_count;
    out->normal_limit = record->normal_limit;
    out->hard_limit = record->hard_limit;
    out->workdir_parent_dev = record->workdir_parent_dev;
    out->workdir_parent_ino = record->workdir_parent_ino;
    out->workdir_dev = record->workdir_dev;
    out->workdir_ino = record->workdir_ino;
    out->workdir_bound = record->workdir_bound;
    out->no_dependent_artifact = record->no_dependent_artifact;
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
    (void)memcpy(out->workdir_name, record->workdir_name,
        CPL_WORKDIR_NAME_SIZE);
    (void)memcpy(out->descriptors, record->descriptors,
        sizeof(out->descriptors));
}

static void preserve_allocation(struct cpl_state *out,
    const struct cpl_state *current) {
    out->normal_limit = current->normal_limit;
    out->hard_limit = current->hard_limit;
    out->workdir_parent_dev = current->workdir_parent_dev;
    out->workdir_parent_ino = current->workdir_parent_ino;
    out->workdir_dev = current->workdir_dev;
    out->workdir_ino = current->workdir_ino;
    out->workdir_bound = current->workdir_bound;
    out->no_dependent_artifact = current->no_dependent_artifact;
    (void)memcpy(out->workdir_name, current->workdir_name,
        CPL_WORKDIR_NAME_SIZE);
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

static uint32_t descriptor_step(uint32_t kind) {
    switch (kind) {
    case CPL_DESCRIPTOR_PROCESS_ABSENT:
        return CPL_STEP_PROCESS_ABSENT;
    case CPL_DESCRIPTOR_REAP_PROCESS:
        return CPL_STEP_EXECUTOR_REAPED;
    case CPL_DESCRIPTOR_REMOVE_WORKDIR:
        return CPL_STEP_WORKDIR_REMOVED;
    case CPL_DESCRIPTOR_TERMINAL_CHECKS:
        return CPL_STEP_TERMINAL_CHECKS;
    default:
        return 0U;
    }
}

static uint32_t descriptor_required(uint32_t kind) {
    switch (kind) {
    case CPL_DESCRIPTOR_PROCESS_ABSENT:
        return 0U;
    case CPL_DESCRIPTOR_REAP_PROCESS:
        return CPL_STEP_PROCESS_ABSENT;
    case CPL_DESCRIPTOR_REMOVE_WORKDIR:
        return CPL_STEP_PROCESS_ABSENT | CPL_STEP_EXECUTOR_REAPED;
    case CPL_DESCRIPTOR_TERMINAL_CHECKS:
        return CPL_STEP_PROCESS_ABSENT | CPL_STEP_EXECUTOR_REAPED |
            CPL_STEP_WORKDIR_REMOVED;
    default:
        return UINT32_MAX;
    }
}

static int validate_descriptors(const struct cpl_batch_descriptor *descriptors,
    uint32_t count, uint64_t completed_steps) {
    uint32_t index;
    uint32_t expected = (uint32_t)completed_steps;

    if (descriptors == NULL || count == 0U ||
        count > CPL_MAX_BATCH_DESCRIPTORS ||
        completed_steps > CPL_ALL_COMPLETED_STEPS) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    for (index = 0U; index < count; ++index) {
        uint32_t step = descriptor_step(descriptors[index].kind);
        uint32_t required = descriptor_required(descriptors[index].kind);

        if (step == 0U || required == UINT32_MAX ||
            descriptors[index].required_steps != required ||
            (expected & required) != required || (expected & step) != 0U) {
            return CPL_ERR_PRECONDITION;
        }
        if ((descriptors[index].kind == CPL_DESCRIPTOR_PROCESS_ABSENT ||
             descriptors[index].kind == CPL_DESCRIPTOR_REAP_PROCESS) &&
            !complete_identity(&descriptors[index].target)) {
            return CPL_ERR_PROCESS_IDENTITY;
        }
        if (descriptors[index].kind == CPL_DESCRIPTOR_REAP_PROCESS &&
            index > 0U &&
            !same_process_identity(&descriptors[index - 1U].target,
                &descriptors[index].target)) {
            return CPL_ERR_PROCESS_IDENTITY;
        }
        expected |= step;
    }
    return CPL_OK;
}

static int validate_record_descriptors(const struct cpl_record *record) {
    if (record->kind == CPL_RECORD_BATCH_ACTIVE) {
        uint32_t index;
        int status = validate_descriptors(record->descriptors,
            record->descriptor_count, record->completed_steps);

        if (status != CPL_OK) {
            return status;
        }
        for (index = record->descriptor_count;
             index < CPL_MAX_BATCH_DESCRIPTORS; ++index) {
            if (!is_zero((const uint8_t *)&record->descriptors[index],
                    sizeof(record->descriptors[index]))) {
                return CPL_ERR_PRECONDITION;
            }
        }
        return CPL_OK;
    }
    if (record->descriptor_count != 0U ||
        !is_zero((const uint8_t *)record->descriptors,
            sizeof(record->descriptors))) {
        return CPL_ERR_PRECONDITION;
    }
    return CPL_OK;
}

static uint64_t completed_descriptor_steps(const struct cpl_state *state) {
    uint64_t completed = state->completed_steps;
    uint32_t index;

    for (index = 0U; index < state->descriptor_count; ++index) {
        completed |= descriptor_step(state->descriptors[index].kind);
    }
    return completed;
}

static int validate_temporal_admission(const struct cpl_state *current,
    const struct cpl_record *record, uint64_t now) {
    switch (record->kind) {
    case CPL_RECORD_PREPARED:
        return record->deadline_ns > now ? CPL_OK : CPL_ERR_AUTHORITY;
    case CPL_RECORD_ACTIVE_READY:
        return current->deadline_ns > now &&
            record->lease_deadline_ns > now ? CPL_OK : CPL_ERR_AUTHORITY;
    case CPL_RECORD_BATCH_ACTIVE:
        return current->lease_deadline_ns > now ? CPL_OK : CPL_ERR_AUTHORITY;
    case CPL_RECORD_RETIRING_IDLE:
    case CPL_RECORD_RETIRING_BATCH:
        if (record->deadline_ns <= now) {
            return CPL_ERR_AUTHORITY;
        }
        if (current->kind == CPL_STATE_PREPARED) {
            return current->deadline_ns <= now ? CPL_OK : CPL_ERR_AUTHORITY;
        }
        return current->lease_deadline_ns <= now ? CPL_OK : CPL_ERR_AUTHORITY;
    case CPL_RECORD_REPLACE_AUTHORITY:
        return current->deadline_ns <= now && record->deadline_ns > now ?
            CPL_OK : CPL_ERR_AUTHORITY;
    default:
        return CPL_OK;
    }
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
    if (validate_record_descriptors(record) != CPL_OK) {
        return CPL_ERR_ILLEGAL_TRANSITION;
    }

    switch (record->kind) {
    case CPL_RECORD_WORKDIR_BOUND:
        if (current->kind != CPL_STATE_NO_GENERATION ||
            current->workdir_bound != 0U ||
            record->workdir_bound != 1U || record->workdir_dev == 0U ||
            record->workdir_ino == 0U ||
            record->workdir_parent_dev != current->workdir_parent_dev ||
            record->workdir_parent_ino != current->workdir_parent_ino ||
            memcmp(record->workdir_name, current->workdir_name,
                CPL_WORKDIR_NAME_SIZE) != 0) {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        next = *current;
        next.workdir_bound = 1U;
        next.workdir_dev = record->workdir_dev;
        next.workdir_ino = record->workdir_ino;
        break;
    case CPL_RECORD_NO_DEPENDENT_ARTIFACT:
        if (current->kind != CPL_STATE_NO_GENERATION ||
            current->workdir_bound != 0U ||
            current->no_dependent_artifact != 0U ||
            record->no_dependent_artifact != 1U) {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        next = *current;
        next.no_dependent_artifact = 1U;
        break;
    case CPL_RECORD_PREPARED:
        if (!has_id(record->candidate) || record->generation == 0U ||
            current->workdir_bound != 1U || current->workdir_dev == 0U ||
            current->workdir_ino == 0U ||
            current->no_dependent_artifact != 0U) {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        if (current->kind == CPL_STATE_NO_GENERATION) {
            state_from_record(&next, record, CPL_STATE_PREPARED);
            preserve_allocation(&next, current);
        } else if (current->kind == CPL_STATE_RETIRING_IDLE &&
            record->generation == current->generation + 1U) {
            state_from_record(&next, record, CPL_STATE_PREPARED);
            preserve_allocation(&next, current);
            if (current->batch_outcome == CPL_BATCH_INTERRUPTED) {
                (void)memcpy(next.inherited_batch, current->exact_batch,
                    CPL_ID_SIZE);
            }
        } else {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        break;
    case CPL_RECORD_ACTIVE_READY:
        if (current->kind != CPL_STATE_PREPARED ||
            record->generation != current->generation ||
            !same_id(record->executor, current->candidate) ||
            current->workdir_bound != 1U || current->workdir_dev == 0U ||
            current->workdir_ino == 0U) {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        state_from_record(&next, record, CPL_STATE_ACTIVE_READY);
        preserve_allocation(&next, current);
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
        preserve_allocation(&next, current);
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
            record->batch_outcome == CPL_BATCH_NONE &&
            record->completed_steps == completed_descriptor_steps(current)) {
            state_from_record(&next, record, CPL_STATE_ACTIVE_READY);
            preserve_allocation(&next, current);
            preserve_process_identity(&next, current);
            (void)memcpy(next.inherited_batch, current->inherited_batch,
                CPL_ID_SIZE);
            (void)memset(next.exact_batch, 0, CPL_ID_SIZE);
        } else if (current->kind == CPL_STATE_RETIRING_BATCH &&
            ((record->batch_outcome == CPL_BATCH_COMPLETED &&
              record->completed_steps ==
                  completed_descriptor_steps(current)) ||
             (record->batch_outcome == CPL_BATCH_INTERRUPTED &&
              record->completed_steps == current->completed_steps))) {
            next = *current;
            next.kind = CPL_STATE_RETIRING_IDLE;
            next.batch_outcome = record->batch_outcome;
            next.completed_steps = record->completed_steps;
            if (record->batch_outcome == CPL_BATCH_COMPLETED) {
                (void)memset(next.inherited_batch, 0, CPL_ID_SIZE);
            } else {
                (void)memcpy(next.inherited_batch, current->exact_batch,
                    CPL_ID_SIZE);
            }
        } else {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        break;
    case CPL_RECORD_RETIRING_IDLE:
        if (record->generation != current->generation ||
            !has_id(record->authority) ||
            current->authority_epoch != 0U ||
            record->authority_epoch != CPL_INITIAL_AUTHORITY_EPOCH) {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        if (current->kind == CPL_STATE_PREPARED &&
            same_id(record->prior_actor, current->candidate)) {
            state_from_record(&next, record, CPL_STATE_RETIRING_IDLE);
            preserve_allocation(&next, current);
            (void)memset(next.exact_batch, 0, CPL_ID_SIZE);
            (void)memset(next.inherited_batch, 0, CPL_ID_SIZE);
            next.batch_outcome = CPL_BATCH_NONE;
            next.descriptor_count = 0U;
            (void)memset(next.descriptors, 0, sizeof(next.descriptors));
        } else if (current->kind == CPL_STATE_ACTIVE_READY &&
            same_id(record->prior_actor, current->executor)) {
            state_from_record(&next, record, CPL_STATE_RETIRING_IDLE);
            preserve_allocation(&next, current);
            next.completed_steps = current->completed_steps;
            preserve_process_identity(&next, current);
            (void)memset(next.exact_batch, 0, CPL_ID_SIZE);
            (void)memset(next.inherited_batch, 0, CPL_ID_SIZE);
            next.batch_outcome = CPL_BATCH_NONE;
            next.descriptor_count = 0U;
            (void)memset(next.descriptors, 0, sizeof(next.descriptors));
        } else {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        break;
    case CPL_RECORD_RETIRING_BATCH:
        if (current->kind != CPL_STATE_BATCH_ACTIVE ||
            record->generation != current->generation ||
            !same_id(record->prior_actor, current->executor) ||
            !same_id(record->exact_batch, current->exact_batch) ||
            !has_id(record->authority) || current->authority_epoch != 0U ||
            record->authority_epoch != CPL_INITIAL_AUTHORITY_EPOCH) {
            return CPL_ERR_ILLEGAL_TRANSITION;
        }
        state_from_record(&next, record, CPL_STATE_RETIRING_BATCH);
        preserve_allocation(&next, current);
        next.completed_steps = current->completed_steps;
        preserve_process_identity(&next, current);
        (void)memcpy(next.executor, current->executor, CPL_ID_SIZE);
        next.descriptor_count = current->descriptor_count;
        (void)memcpy(next.descriptors, current->descriptors,
            sizeof(next.descriptors));
        break;
    case CPL_RECORD_REPLACE_AUTHORITY:
        if ((current->kind != CPL_STATE_RETIRING_IDLE &&
             current->kind != CPL_STATE_RETIRING_BATCH) ||
            current->authority_epoch != CPL_INITIAL_AUTHORITY_EPOCH ||
            record->authority_epoch != CPL_MAX_AUTHORITY_EPOCH ||
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
    out->unhealthy = journal->unhealthy ||
        out->physical_eof > journal->hard_limit;
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

        if (valid_physical_bootstrap_record(journal, candidate, remaining)) {
            offset += CPL_BOOTSTRAP_RECORD_SIZE;
            continue;
        }
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
        if (get_u64(candidate + 56U) != record.cleanup_epoch ||
            get_u32(candidate + 96U) != record.kind ||
            memcmp(candidate + 64U, record.parent_hash,
                CPL_HASH_SIZE) != 0) {
            ++out->invalid_bytes;
            ++offset;
            continue;
        }
        if (!out->has_intent) {
            if (sequence == 0U && record.kind == CPL_RECORD_INTENT &&
                is_zero(candidate + 64U, CPL_HASH_SIZE) &&
                record.normal_limit == journal->normal_limit &&
                record.hard_limit == journal->hard_limit &&
                record.workdir_parent_dev ==
                    (uint64_t)journal->workdir_parent_dev &&
                record.workdir_parent_ino ==
                    (uint64_t)journal->workdir_parent_ino &&
                memcmp(record.workdir_name, journal->workdir_name,
                    CPL_WORKDIR_NAME_SIZE) == 0 &&
                record.workdir_bound == 0U &&
                record.no_dependent_artifact == 0U) {
                out->has_intent = true;
                out->head_sequence = 0U;
                out->canonical_records = 1U;
                out->state.normal_limit = record.normal_limit;
                out->state.hard_limit = record.hard_limit;
                out->state.workdir_parent_dev = record.workdir_parent_dev;
                out->state.workdir_parent_ino = record.workdir_parent_ino;
                (void)memcpy(out->state.workdir_name, record.workdir_name,
                    CPL_WORKDIR_NAME_SIZE);
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
    if (out->physical_eof <= journal->hard_limit &&
        journal->hard_limit - out->physical_eof < CPL_PHYSICAL_RECORD_SIZE &&
        out->state.kind != CPL_STATE_DONE) {
        out->unhealthy = true;
    }
    free(bytes);
    return CPL_OK;
}

#ifdef CPL_ENABLE_FAULT_INJECTION
static int certify_notify_fd = -1;
static int certify_wait_fd = -1;
static uint32_t create_pause_point = 0U;
static int create_pause_notify_fd = -1;
static int create_pause_wait_fd = -1;
static uint64_t create_pause_deadline_ns = 0U;

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

static uint64_t fault_create_deadline(void) {
    return create_pause_deadline_ns;
}

static int fault_create_pause(uint32_t point) {
    int notify_fd = -1;
    int wait_fd = -1;
    int status = CPL_OK;

    if (create_pause_point == point) {
        notify_fd = create_pause_notify_fd;
        wait_fd = create_pause_wait_fd;
        create_pause_point = 0U;
        create_pause_notify_fd = -1;
        create_pause_wait_fd = -1;
        create_pause_deadline_ns = 0U;
    }
    if (notify_fd >= 0 && wait_fd >= 0) {
        status = checked_byte_write(notify_fd);
        if (status == CPL_OK) {
            status = checked_byte_read(wait_fd);
        }
    }
    return status;
}

static int fault_lifecycle_pause(cpl_journal *journal, uint32_t point) {
    int status;

    if (journal->pause_point != point) {
        return CPL_OK;
    }
    status = checked_byte_write(journal->pause_notify_fd);
    if (status == CPL_OK) {
        status = checked_byte_read(journal->pause_wait_fd);
    }
    journal->pause_point = 0U;
    journal->pause_notify_fd = -1;
    journal->pause_wait_fd = -1;
    return status;
}
#else
static void fault_exit(const char *point) {
    (void)point;
}

static int fault_certify_pause(void) {
    return CPL_OK;
}

static uint64_t fault_create_deadline(void) {
    return 0U;
}

static int fault_create_pause(uint32_t point) {
    (void)point;
    return CPL_OK;
}


static int fault_lifecycle_pause(cpl_journal *journal, uint32_t point) {
    (void)journal;
    (void)point;
    return CPL_OK;
}
#endif

static int preallocate_file(int fd, uint64_t length, uint64_t deadline) {
    struct fstore allocation;
    int status;

    if (length > (uint64_t)INT64_MAX) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(&allocation, 0, sizeof(allocation));
    allocation.fst_flags = F_ALLOCATECONTIG;
    allocation.fst_posmode = F_PEOFPOSMODE;
    allocation.fst_length = (off_t)length;
    status = fault_create_pause(CPL_FAULT_BEFORE_CREATE_PREALLOCATE);
    if (status != CPL_OK || monotonic_ns() >= deadline) {
        return status != CPL_OK ? status : CPL_ERR_LOCK_TIMEOUT;
    }
    if (fcntl(fd, F_PREALLOCATE, &allocation) == 0) {
        return CPL_OK;
    }
    allocation.fst_flags = F_ALLOCATEALL;
    if (monotonic_ns() >= deadline) {
        return CPL_ERR_LOCK_TIMEOUT;
    }
    if (fcntl(fd, F_PREALLOCATE, &allocation) < 0) {
        return CPL_ERR_SYSTEM;
    }
    return CPL_OK;
}

int cpl_journal_create_at(int parent_dirfd, const char *journal_name,
    int workdir_parent_dirfd, const char *workdir_name,
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
    uint64_t deadline = effective_deadline(fault_create_deadline());
    bool mutex_held = false;
    bool flock_held = false;

    if (out == NULL || receipt == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    *out = NULL;
    (void)memset(receipt, 0, sizeof(*receipt));
    status = validate_component(journal_name);
    if (status != CPL_OK ||
        validate_derived_workdir_name(journal_name, workdir_name) != CPL_OK ||
        nonce == NULL || validate_limits(normal_limit, hard_limit) != CPL_OK) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    status = validate_parent(parent_dirfd, &parent_stat);
    if (status != CPL_OK) {
        return status;
    }
    status = fault_create_pause(CPL_FAULT_BEFORE_CREATE_OPENAT);
    if (status != CPL_OK || monotonic_ns() >= deadline) {
        return status != CPL_OK ? status : CPL_ERR_LOCK_TIMEOUT;
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
    status = initialize_handle(fd, parent_dirfd, journal_name,
        workdir_parent_dirfd, workdir_name, nonce, normal_limit, hard_limit,
        &journal);
    if (status != CPL_OK) {
        (void)close(fd);
        return status;
    }
    status = preallocate_file(fd, hard_limit, deadline);
    if (status != CPL_OK) {
        cpl_journal_close(journal);
        return status;
    }
    fault_exit("after_preallocate");
    (void)memset(&intent, 0, sizeof(intent));
    intent.kind = CPL_RECORD_INTENT;
    intent.normal_limit = normal_limit;
    intent.hard_limit = hard_limit;
    intent.workdir_parent_dev = (uint64_t)journal->workdir_parent_dev;
    intent.workdir_parent_ino = (uint64_t)journal->workdir_parent_ino;
    (void)memcpy(intent.workdir_name, journal->workdir_name,
        CPL_WORKDIR_NAME_SIZE);
    status = encode_record(journal, &intent, 0U, zero_hash, encoded,
        sizeof(encoded), &encoded_length, intent_hash);
    if (status != CPL_OK) {
        cpl_journal_close(journal);
        return status;
    }
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
    status = fault_create_pause(CPL_FAULT_BEFORE_CREATE_INTENT_WRITE);
    if (status != CPL_OK || monotonic_ns() >= deadline) {
        (void)release_flock(journal->append_lock_fd);
        (void)pthread_mutex_unlock(&journal->append_mutex);
        cpl_journal_close(journal);
        return status != CPL_OK ? status : CPL_ERR_LOCK_TIMEOUT;
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
    status = fault_create_pause(CPL_FAULT_BEFORE_CREATE_FULLFSYNC);
    if (status != CPL_OK || monotonic_ns() >= deadline) {
        cpl_journal_close(journal);
        return status != CPL_OK ? status : CPL_ERR_LOCK_TIMEOUT;
    }
    if (fcntl(fd, F_FULLFSYNC) < 0) {
        cpl_journal_close(journal);
        return CPL_ERR_SYSTEM;
    }
    fault_exit("after_journal_fullfsync");
    status = validate_parent_identity(journal, parent_dirfd);
    if (status == CPL_OK) {
        status = fault_create_pause(CPL_FAULT_BEFORE_CREATE_PARENT_FSYNC);
    }
    if (status == CPL_OK && monotonic_ns() >= deadline) {
        status = CPL_ERR_LOCK_TIMEOUT;
    }
    if (status != CPL_OK || fsync(parent_dirfd) < 0) {
        cpl_journal_close(journal);
        return status == CPL_OK ? CPL_ERR_SYSTEM : status;
    }
    fault_exit("after_intent_parent_fsync");
    receipt->state = CPL_INTENT_PARENT_DIRSYNCED;
    (void)memcpy(receipt->intent_hash, intent_hash, CPL_HASH_SIZE);
    (void)memcpy(receipt->capability, journal->create_capability,
        CPL_HASH_SIZE);
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
    int workdir_parent_dirfd, const char *workdir_name,
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
    if (status != CPL_OK ||
        validate_derived_workdir_name(journal_name, workdir_name) != CPL_OK ||
        nonce == NULL || validate_limits(normal_limit, hard_limit) != CPL_OK) {
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
    status = initialize_handle(fd, parent_dirfd, journal_name,
        workdir_parent_dirfd, workdir_name, nonce, normal_limit, hard_limit,
        &journal);
    if (status == CPL_OK) {
        status = first_record_nonce_status(fd, nonce);
    }
    if (status == CPL_OK) {
        status = scan_internal(journal, &chain);
    }
    if (status == CPL_OK && chain.physical_eof >=
            CPL_HEADER_SIZE + sizeof(struct cpl_record) && !chain.has_intent) {
        status = CPL_ERR_IDENTITY_DRIFT;
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
    uint64_t deadline = effective_deadline(0U);
    bool flock_held = false;

    if (status != CPL_OK) {
        return status;
    }
    if (out == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    status = take_mutex(&journal->append_mutex, deadline);
    if (status != CPL_OK) {
        return status;
    }
    status = take_flock(journal->append_lock_fd, deadline);
    if (status == CPL_OK) {
        flock_held = true;
        status = scan_internal(journal, out);
    }
    if (flock_held && release_flock(journal->append_lock_fd) != CPL_OK &&
        status == CPL_OK) {
        status = CPL_ERR_SYSTEM;
    }
    (void)pthread_mutex_unlock(&journal->append_mutex);
    return status;
}

static bool recovery_allowed(const struct cpl_chain *chain,
    const struct cpl_record *record) {
    if (record->kind == CPL_RECORD_RETIRING_IDLE ||
        record->kind == CPL_RECORD_RETIRING_BATCH ||
        record->kind == CPL_RECORD_BATCH_DONE ||
        record->kind == CPL_RECORD_UNCONFIRMED ||
        record->kind == CPL_RECORD_REPLACE_AUTHORITY ||
        record->kind == CPL_RECORD_NO_DEPENDENT_ARTIFACT ||
        (record->authority_epoch != 0U &&
         (record->kind == CPL_RECORD_ACTIVE_READY ||
          record->kind == CPL_RECORD_BATCH_ACTIVE ||
          record->kind == CPL_RECORD_DONE))) {
        return true;
    }
    return record->kind == CPL_RECORD_PREPARED &&
        chain->state.kind == CPL_STATE_RETIRING_IDLE;
}

int cpl_journal_append(cpl_journal *journal, const uint8_t *record_bytes,
    uint32_t record_len, uint32_t record_class, uint64_t deadline_ns,
    struct cpl_append_result *out) {
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

    if (record_bytes == NULL || out == NULL ||
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
    (void)memset(out, 0, sizeof(*out));
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
    if ((record.kind == CPL_RECORD_ACTIVE_READY ||
         record.kind == CPL_RECORD_BATCH_ACTIVE ||
         record.kind == CPL_RECORD_BATCH_DONE ||
         record.kind == CPL_RECORD_RETIRING_IDLE ||
         record.kind == CPL_RECORD_RETIRING_BATCH ||
         record.kind == CPL_RECORD_DONE ||
         record.kind == CPL_RECORD_REPLACE_AUTHORITY ||
         record.kind == CPL_RECORD_UNCONFIRMED ||
         record.kind == CPL_RECORD_WORKDIR_BOUND ||
         record.kind == CPL_RECORD_NO_DEPENDENT_ARTIFACT) &&
        (journal->authorized_record_kind != record.kind ||
         !pthread_equal(journal->authorized_thread, pthread_self()))) {
        status = CPL_ERR_AUTHORITY;
        goto done;
    }
    if (record.kind == CPL_RECORD_PREPARED &&
        (record.deadline_ns <= monotonic_ns() ||
         record.completed_steps != 0U || record.process_pid != 0)) {
        status = CPL_ERR_AUTHORITY;
        goto done;
    }
    if (record.kind == CPL_RECORD_PREPARED &&
        chain.state.kind == CPL_STATE_RETIRING_IDLE &&
        (journal->authorized_record_kind != CPL_RECORD_PREPARED ||
         !pthread_equal(journal->authorized_thread, pthread_self()))) {
        status = CPL_ERR_AUTHORITY;
        goto done;
    }
    if (record.kind == CPL_RECORD_REPLACE_AUTHORITY &&
        chain.state.deadline_ns > monotonic_ns()) {
        status = CPL_ERR_AUTHORITY;
        goto done;
    }
    status = validate_temporal_admission(&chain.state, &record,
        monotonic_ns());
    if (status != CPL_OK) {
        goto done;
    }
    if (!is_zero(record.parent_hash, CPL_HASH_SIZE) &&
        memcmp(record.parent_hash, chain.head_hash, CPL_HASH_SIZE) != 0) {
        status = CPL_ERR_PARENT_MISMATCH;
        goto done;
    }
    record.normal_limit = chain.state.normal_limit;
    record.hard_limit = chain.state.hard_limit;
    record.workdir_parent_dev = chain.state.workdir_parent_dev;
    record.workdir_parent_ino = chain.state.workdir_parent_ino;
    if (record.kind != CPL_RECORD_WORKDIR_BOUND) {
        record.workdir_dev = chain.state.workdir_dev;
        record.workdir_ino = chain.state.workdir_ino;
        record.workdir_bound = chain.state.workdir_bound;
    }
    if (record.kind != CPL_RECORD_NO_DEPENDENT_ARTIFACT) {
        record.no_dependent_artifact = chain.state.no_dependent_artifact;
    }
    (void)memcpy(record.workdir_name, chain.state.workdir_name,
        CPL_WORKDIR_NAME_SIZE);
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
        chain.head_hash, encoded, sizeof(encoded), &encoded_length, out->hash);
    if (status != CPL_OK) {
        goto done;
    }
    status = validate_temporal_admission(&chain.state, &record,
        monotonic_ns());
    if (status != CPL_OK) {
        goto done;
    }
    if (monotonic_ns() >= deadline) {
        status = CPL_ERR_LOCK_TIMEOUT;
        goto done;
    }
    written = write(journal->fd, encoded, encoded_length);
    if (written < 0 || (size_t)written != encoded_length) {
        journal->unhealthy = true;
        status = CPL_ERR_IO_SHORT;
        goto done;
    }
    out->state = next;
    out->sequence = chain.head_sequence + 1U;
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
        if (snapshot.unhealthy) {
            return CPL_ERR_CORRUPT;
        }
        status = fault_certify_pause();
        if (status != CPL_OK) {
            return status;
        }
        if (fcntl(journal->fd, F_FULLFSYNC) < 0) {
            return CPL_ERR_SYSTEM;
        }
        if (monotonic_ns() >= deadline) {
            return CPL_ERR_CERTIFY_TIMEOUT;
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
        if (verified.unhealthy) {
            return CPL_ERR_CORRUPT;
        }
        if (snapshot.physical_eof == verified.physical_eof &&
            memcmp(snapshot.head_hash, verified.head_hash, CPL_HASH_SIZE) == 0) {
            out->state = verified.state;
            (void)memcpy(out->head_hash, verified.head_hash, CPL_HASH_SIZE);
            out->head_sequence = verified.head_sequence;
            out->physical_eof = verified.physical_eof;
            out->attempts = attempts;
            out->has_intent = verified.has_intent;
            out->done_authority = verified.has_intent &&
                verified.state.kind == CPL_STATE_DONE &&
                verified.state.completed_steps == CPL_ALL_COMPLETED_STEPS &&
                journal->reap_proof_valid &&
                memcmp(journal->reap_head_hash, verified.head_hash,
                    CPL_HASH_SIZE) == 0;
            out->partial_create_authority =
                (!verified.has_intent && verified.physical_eof == 0U) ||
                (verified.has_intent &&
                 verified.state.no_dependent_artifact != 0U);
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
            CPL_HASH_SIZE) != 0 ||
        !same_capability(authority->capability,
            journal->reap_capability)) {
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
        return certified->done_authority && journal->reap_proof_valid ?
            CPL_OK : CPL_ERR_AUTHORITY;
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

static int validate_requested_workdir(cpl_journal *journal,
    int workdir_parent_dirfd, const char *workdir_name) {
    int status;

    if (workdir_name == NULL ||
        strcmp(workdir_name, journal->workdir_name) != 0) {
        return CPL_ERR_IDENTITY_DRIFT;
    }
    status = validate_workdir_parent_identity(journal, workdir_parent_dirfd);
    return status;
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

static int append_authorized(cpl_journal *journal, struct cpl_record *record,
    uint32_t record_class, uint64_t deadline,
    struct cpl_append_result *out) {
    int status;

    journal->authorized_record_kind = record->kind;
    journal->authorized_thread = pthread_self();
    status = cpl_journal_append(journal, (const uint8_t *)record,
        (uint32_t)sizeof(*record), record_class, deadline, out);
    journal->authorized_record_kind = 0U;
    (void)memset(&journal->authorized_thread, 0,
        sizeof(journal->authorized_thread));
    return status;
}

int cpl_journal_mark_unconfirmed(cpl_journal *journal, uint32_t reason_code,
    uint64_t deadline_ns, struct cpl_append_result *out) {
    static const char proof_unavailable[] = "proof unavailable";
    static const char normal_exhausted[] = "normal region exhausted";
    static const char identity_unavailable[] = "identity unavailable";
    const char *reason;
    struct cpl_certified_head certified;
    struct cpl_record record;
    uint64_t deadline = effective_deadline(deadline_ns);
    int status;

    if (out == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    switch (reason_code) {
    case CPL_UNCONFIRMED_PROOF_UNAVAILABLE:
        reason = proof_unavailable;
        break;
    case CPL_UNCONFIRMED_NORMAL_REGION_EXHAUSTED:
        reason = normal_exhausted;
        break;
    case CPL_UNCONFIRMED_IDENTITY_UNAVAILABLE:
        reason = identity_unavailable;
        break;
    default:
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(&record, 0, sizeof(record));
    record.kind = CPL_RECORD_UNCONFIRMED;
    (void)memcpy(record.reason, reason, strlen(reason));
    if (monotonic_ns() >= deadline) {
        return CPL_ERR_LOCK_TIMEOUT;
    }
    status = lock_action(journal, deadline);
    if (status != CPL_OK) {
        return status;
    }
    status = cpl_journal_certify(journal, deadline, &certified);
    if (status != CPL_OK) {
        goto done;
    }
    if (certified.state.kind == CPL_STATE_BATCH_ACTIVE ||
        certified.state.kind == CPL_STATE_RETIRING_BATCH ||
        journal->action_held) {
        status = CPL_ERR_AUTHORITY;
        goto done;
    }
    status = monotonic_ns() >= deadline ? CPL_ERR_LOCK_TIMEOUT :
        append_authorized(journal, &record, CPL_RECORD_RECOVERY, deadline,
            out);

done:
    unlock_action(journal);
    return status;
}

static int validate_workdir_parent_identity(cpl_journal *journal,
    int parent_dirfd) {
    struct stat parent;
    int status = validate_parent(parent_dirfd, &parent);

    if (status != CPL_OK) {
        return status;
    }
    if (parent.st_dev != journal->workdir_parent_dev ||
        parent.st_ino != journal->workdir_parent_ino) {
        return CPL_ERR_IDENTITY_DRIFT;
    }
    return CPL_OK;
}

static int require_dependent_namespace_absent(cpl_journal *journal,
    int parent_dirfd) {
    static const char suffix[] = ".journal";
    char append_lock[CPL_MAX_NAME + 32U];
    char action_lock[CPL_MAX_NAME + 32U];
    size_t journal_length = strlen(journal->journal_name);
    size_t stem_length = journal_length - (sizeof(suffix) - 1U);
    DIR *directory;
    struct dirent *entry;
    int scan_fd;
    int status = CPL_OK;

    if (snprintf(append_lock, sizeof(append_lock), "%s.append.lock",
            journal->journal_name) < 0 ||
        snprintf(action_lock, sizeof(action_lock), "%s.action.lock",
            journal->journal_name) < 0) {
        return CPL_ERR_SYSTEM;
    }
    scan_fd = openat(parent_dirfd, ".",
        O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (scan_fd < 0) {
        return CPL_ERR_SYSTEM;
    }
    status = validate_workdir_parent_identity(journal, scan_fd);
    if (status != CPL_OK) {
        (void)close(scan_fd);
        return status;
    }
    directory = fdopendir(scan_fd);
    if (directory == NULL) {
        (void)close(scan_fd);
        return CPL_ERR_SYSTEM;
    }
    errno = 0;
    while ((entry = readdir(directory)) != NULL) {
        const char *name = entry->d_name;

        if (strncmp(name, journal->journal_name, stem_length) != 0 ||
            name[stem_length] != '.') {
            continue;
        }
        if (strcmp(name, journal->journal_name) == 0 ||
            strcmp(name, append_lock) == 0 || strcmp(name, action_lock) == 0) {
            continue;
        }
        status = CPL_ERR_WORKDIR_PRESENT;
        break;
    }
    if (status == CPL_OK && errno != 0) {
        status = CPL_ERR_SYSTEM;
    }
    (void)closedir(directory);
    return status;
}

static int certify_exact_result(cpl_journal *journal,
    const struct cpl_append_result *appended, uint64_t deadline,
    struct cpl_certified_head *certified) {
    int status = cpl_journal_certify(journal, deadline, certified);

    if (status != CPL_OK) {
        return status;
    }
    if (memcmp(certified->head_hash, appended->hash, CPL_HASH_SIZE) != 0) {
        return CPL_ERR_AUTHORITY;
    }
    return CPL_OK;
}

int cpl_journal_create_workdir(cpl_journal *journal,
    int workdir_parent_dirfd, const struct cpl_create_receipt *create_receipt,
    uint64_t deadline_ns, struct cpl_workdir_receipt *receipt) {
    struct cpl_certified_head before;
    struct cpl_certified_head after;
    struct cpl_append_result appended;
    struct cpl_record record;
    struct stat directory;
    struct stat path_identity;
    uint64_t deadline = effective_deadline(deadline_ns);
    int directory_fd = -1;
    int status;
    bool action_locked = false;

    if (receipt == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(receipt, 0, sizeof(*receipt));
    status = ensure_owner(journal);
    if (status != CPL_OK || create_receipt == NULL ||
        create_receipt->state != CPL_INTENT_PARENT_DIRSYNCED ||
        !same_capability(create_receipt->capability,
            journal->create_capability)) {
        return status == CPL_OK ? CPL_ERR_RECEIPT : status;
    }
    status = validate_workdir_parent_identity(journal, workdir_parent_dirfd);
    if (status != CPL_OK) {
        return status;
    }
    status = lock_action(journal, deadline);
    if (status != CPL_OK) {
        return status;
    }
    action_locked = true;
    status = cpl_journal_certify(journal, deadline, &before);
    if (status != CPL_OK || !before.has_intent ||
        before.state.kind != CPL_STATE_NO_GENERATION ||
        before.state.workdir_bound != 0U ||
        memcmp(before.head_hash, create_receipt->intent_hash,
            CPL_HASH_SIZE) != 0) {
        status = status == CPL_OK ? CPL_ERR_RECEIPT : status;
        goto done;
    }
    if (monotonic_ns() >= deadline) {
        status = CPL_ERR_LOCK_TIMEOUT;
        goto done;
    }
    if (mkdirat(workdir_parent_dirfd, journal->workdir_name,
            (mode_t)0700) < 0) {
        status = errno == EEXIST ? CPL_ERR_WORKDIR_PRESENT : CPL_ERR_SYSTEM;
        goto done;
    }
    directory_fd = openat(workdir_parent_dirfd, journal->workdir_name,
        O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (directory_fd < 0 || fstat(directory_fd, &directory) < 0 ||
        !S_ISDIR(directory.st_mode) || directory.st_uid != geteuid() ||
        (directory.st_mode & (mode_t)0777) != (mode_t)0700 ||
        directory.st_dev != journal->workdir_parent_dev) {
        status = CPL_ERR_UNSAFE_FILE;
        goto done;
    }
    if (monotonic_ns() >= deadline) {
        status = CPL_ERR_LOCK_TIMEOUT;
        goto done;
    }
    if (fsync(workdir_parent_dirfd) < 0) {
        status = CPL_ERR_SYSTEM;
        goto done;
    }
    status = fault_lifecycle_pause(journal,
        CPL_FAULT_BEFORE_WORKDIR_BOUND_APPEND);
    if (status != CPL_OK) {
        goto done;
    }
    if (fstatat(workdir_parent_dirfd, journal->workdir_name, &path_identity,
            AT_SYMLINK_NOFOLLOW) < 0 || !S_ISDIR(path_identity.st_mode) ||
        path_identity.st_uid != geteuid() ||
        (path_identity.st_mode & (mode_t)0777) != (mode_t)0700 ||
        path_identity.st_dev != directory.st_dev ||
        path_identity.st_ino != directory.st_ino ||
        monotonic_ns() >= deadline) {
        status = monotonic_ns() >= deadline ? CPL_ERR_LOCK_TIMEOUT :
            CPL_ERR_IDENTITY_DRIFT;
        goto done;
    }
    (void)memset(&record, 0, sizeof(record));
    record.kind = CPL_RECORD_WORKDIR_BOUND;
    record.workdir_bound = 1U;
    record.workdir_parent_dev = (uint64_t)journal->workdir_parent_dev;
    record.workdir_parent_ino = (uint64_t)journal->workdir_parent_ino;
    record.workdir_dev = (uint64_t)directory.st_dev;
    record.workdir_ino = (uint64_t)directory.st_ino;
    (void)memcpy(record.workdir_name, journal->workdir_name,
        CPL_WORKDIR_NAME_SIZE);
    status = append_authorized(journal, &record, CPL_RECORD_NORMAL, deadline,
        &appended);
    if (status == CPL_OK) {
        status = certify_exact_result(journal, &appended, deadline, &after);
    }
    if (status != CPL_OK) {
        goto done;
    }
    receipt->state = CPL_WORKDIR_PARENT_DIRSYNCED;
    (void)memcpy(receipt->bound_hash, after.head_hash, CPL_HASH_SIZE);
    (void)memcpy(receipt->capability, journal->workdir_capability,
        CPL_HASH_SIZE);
    receipt->workdir_dev = (uint64_t)directory.st_dev;
    receipt->workdir_ino = (uint64_t)directory.st_ino;

done:
    if (directory_fd >= 0) {
        (void)close(directory_fd);
    }
    if (action_locked) {
        unlock_action(journal);
    }
    return status;
}

int cpl_journal_certify_no_dependent_artifact(cpl_journal *journal,
    int workdir_parent_dirfd, uint64_t deadline_ns,
    struct cpl_delete_authority *authority) {
    struct cpl_certified_head certified;
    struct cpl_append_result appended;
    struct cpl_record record;
    uint64_t deadline = effective_deadline(deadline_ns);
    int status;

    if (authority == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(authority, 0, sizeof(*authority));
    status = ensure_owner(journal);
    if (status != CPL_OK) {
        return status;
    }
    status = validate_workdir_parent_identity(journal, workdir_parent_dirfd);
    if (status != CPL_OK) {
        return status;
    }
    status = lock_action(journal, deadline);
    if (status != CPL_OK) {
        return status;
    }
    status = require_dependent_namespace_absent(journal,
        workdir_parent_dirfd);
    if (status != CPL_OK) {
        goto done;
    }
    status = fault_lifecycle_pause(journal,
        CPL_FAULT_AFTER_FIRST_DEPENDENT_SCAN);
    if (status != CPL_OK) {
        goto done;
    }
    status = cpl_journal_certify(journal, deadline, &certified);
    if (status != CPL_OK) {
        goto done;
    }
    if (certified.has_intent) {
        if (certified.state.kind != CPL_STATE_NO_GENERATION ||
            certified.state.workdir_bound != 0U ||
            certified.state.no_dependent_artifact != 0U) {
            status = CPL_ERR_AUTHORITY;
            goto done;
        }
        status = require_dependent_namespace_absent(journal,
            workdir_parent_dirfd);
        if (status != CPL_OK || monotonic_ns() >= deadline) {
            status = status != CPL_OK ? status : CPL_ERR_LOCK_TIMEOUT;
            goto done;
        }
        (void)memset(&record, 0, sizeof(record));
        record.kind = CPL_RECORD_NO_DEPENDENT_ARTIFACT;
        record.no_dependent_artifact = 1U;
        status = append_authorized(journal, &record, CPL_RECORD_RECOVERY,
            deadline, &appended);
        if (status == CPL_OK) {
            status = certify_exact_result(journal, &appended, deadline,
                &certified);
        }
    } else if (certified.physical_eof != 0U) {
        status = CPL_ERR_CORRUPT;
    }
    if (status != CPL_OK) {
        goto done;
    }
    status = require_dependent_namespace_absent(journal,
        workdir_parent_dirfd);
    if (status != CPL_OK || monotonic_ns() >= deadline) {
        status = status != CPL_OK ? status : CPL_ERR_LOCK_TIMEOUT;
        goto done;
    }
    random_capability(journal->reap_capability);
    authority->kind = CPL_DELETE_UNRELEASED_PARTIAL_CREATE;
    (void)memcpy(authority->allocation_nonce, journal->nonce, CPL_HASH_SIZE);
    (void)memcpy(authority->certified_hash, certified.head_hash,
        CPL_HASH_SIZE);
    (void)memcpy(authority->capability, journal->reap_capability,
        CPL_HASH_SIZE);

done:
    unlock_action(journal);
    return status;
}

static int observed_identity_status(const struct cpl_process_identity *expected,
    bool *execution_absent) {
    struct proc_bsdinfo process;
    struct cpl_process_identity observed;
    bool have_bsd_info = true;
    int status;

    *execution_absent = false;
    if (!complete_identity(expected)) {
        return CPL_ERR_PROCESS_IDENTITY;
    }
    if (proc_pidinfo((int)expected->pid, PROC_PIDTBSDINFO, 0U, &process,
            (int)sizeof(process)) != (int)sizeof(process)) {
        if (errno == ESRCH) {
            *execution_absent = true;
            return CPL_OK;
        }
#ifdef CPL_ENABLE_FAULT_INJECTION
        if (errno == EPERM) {
            siginfo_t information;

            have_bsd_info = false;
            (void)memset(&information, 0, sizeof(information));
            if (waitid(P_PID, (id_t)expected->pid, &information,
                    WEXITED | WNOHANG | WNOWAIT) == 0 &&
                information.si_pid == expected->pid) {
                *execution_absent = true;
                return CPL_OK;
            }
        } else {
            return CPL_ERR_PROCESS_IDENTITY;
        }
#else
        return CPL_ERR_PROCESS_IDENTITY;
#endif
    }
    status = cpl_process_observe(expected->pid, &observed);
    if (status == CPL_ERR_NOT_FOUND) {
        *execution_absent = true;
        return CPL_OK;
    }
    if (status != CPL_OK) {
        return status;
    }
    if (!same_process_identity(expected, &observed)) {
        *execution_absent = true;
        return CPL_OK;
    }
    if (have_bsd_info && process.pbi_status == SZOMB) {
        *execution_absent = true;
        return CPL_OK;
    }
    return CPL_OK;
}

int cpl_journal_activate_executor(cpl_journal *journal, uint64_t generation,
    const uint8_t executor[CPL_ID_SIZE], int64_t pid,
    uint64_t lease_deadline_ns, uint64_t deadline_ns,
    struct cpl_append_result *out) {
    struct cpl_process_identity identity;
    struct cpl_certified_head certified;
    struct cpl_chain chain;
    struct cpl_record record;
    uint64_t deadline = effective_deadline(deadline_ns);
    int status;

    if (out == NULL || executor == NULL || !has_id(executor) ||
        generation == 0U || lease_deadline_ns <= monotonic_ns()) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    status = cpl_process_observe(pid, &identity);
    if (status != CPL_OK) {
        return status;
    }
    status = lock_action(journal, deadline);
    if (status != CPL_OK) {
        return status;
    }
    status = cpl_journal_scan(journal, &chain);
    if (status != CPL_OK || chain.state.kind != CPL_STATE_PREPARED ||
        chain.state.generation != generation ||
        !same_id(chain.state.candidate, executor) ||
        chain.state.deadline_ns <= monotonic_ns()) {
        status = status == CPL_OK ? CPL_ERR_AUTHORITY : status;
        goto done;
    }
    status = fault_lifecycle_pause(journal,
        CPL_FAULT_BEFORE_ACTIVATION_APPEND);
    if (status != CPL_OK) {
        goto done;
    }
    (void)memset(&record, 0, sizeof(record));
    record.kind = CPL_RECORD_ACTIVE_READY;
    record.generation = generation;
    record.authority_epoch = chain.state.authority_epoch;
    record.lease_deadline_ns = lease_deadline_ns;
    (void)memcpy(record.executor, executor, CPL_ID_SIZE);
    identity_to_record(&identity, &record);
    status = append_authorized(journal, &record,
        record.authority_epoch == 0U ? CPL_RECORD_NORMAL :
            CPL_RECORD_RECOVERY,
        deadline, out);
    if (status == CPL_OK) {
        status = certify_exact_result(journal, out, deadline, &certified);
    }

done:
    unlock_action(journal);
    return status;
}

int cpl_journal_admit_batch(cpl_journal *journal, uint64_t generation,
    const uint8_t executor[CPL_ID_SIZE], const uint8_t batch_nonce[CPL_ID_SIZE],
    const struct cpl_batch_descriptor *descriptors, uint32_t descriptor_count,
    uint64_t deadline_ns, struct cpl_action_token *token,
    struct cpl_append_result *out) {
    struct cpl_process_identity expected;
    struct cpl_process_identity caller;
    struct cpl_certified_head certified;
    struct cpl_chain chain;
    struct cpl_record record;
    uint64_t deadline = effective_deadline(deadline_ns);
    int status;

    if (token == NULL || out == NULL || executor == NULL ||
        batch_nonce == NULL || !has_id(executor) || !has_id(batch_nonce)) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(token, 0, sizeof(*token));
    status = lock_action(journal, deadline);
    if (status != CPL_OK) {
        return status;
    }
    status = cpl_journal_scan(journal, &chain);
    if (status != CPL_OK || chain.unhealthy ||
        chain.state.kind != CPL_STATE_ACTIVE_READY ||
        chain.state.generation != generation ||
        !same_id(chain.state.executor, executor) ||
        chain.state.lease_deadline_ns <= monotonic_ns()) {
        status = status == CPL_OK ? CPL_ERR_AUTHORITY : status;
        goto failed;
    }
    identity_from_state(&chain.state, &expected);
    status = cpl_process_observe((int64_t)getpid(), &caller);
    if (status != CPL_OK || !same_process_identity(&expected, &caller)) {
        status = CPL_ERR_PROCESS_IDENTITY;
        goto failed;
    }
    status = validate_descriptors(descriptors, descriptor_count,
        chain.state.completed_steps);
    if (status != CPL_OK) {
        goto failed;
    }
    (void)memset(&record, 0, sizeof(record));
    record.kind = CPL_RECORD_BATCH_ACTIVE;
    record.generation = generation;
    record.authority_epoch = chain.state.authority_epoch;
    record.lease_deadline_ns = chain.state.lease_deadline_ns;
    record.completed_steps = chain.state.completed_steps;
    record.descriptor_count = descriptor_count;
    (void)memcpy(record.executor, executor, CPL_ID_SIZE);
    (void)memcpy(record.exact_batch, batch_nonce, CPL_ID_SIZE);
    (void)memcpy(record.descriptors, descriptors,
        descriptor_count * sizeof(*descriptors));
    status = append_authorized(journal, &record,
        record.authority_epoch == 0U ? CPL_RECORD_NORMAL :
            CPL_RECORD_RECOVERY,
        deadline, out);
    if (status == CPL_OK) {
        status = fault_lifecycle_pause(journal,
            CPL_FAULT_AFTER_BATCH_ADMISSION_APPEND);
    }
    if (status == CPL_OK) {
        status = cpl_journal_certify(journal, deadline, &certified);
    }
    if (status != CPL_OK ||
        ((certified.state.kind != CPL_STATE_BATCH_ACTIVE &&
          certified.state.kind != CPL_STATE_RETIRING_BATCH) ||
         certified.state.generation != generation ||
         !same_id(certified.state.exact_batch, batch_nonce))) {
        status = status == CPL_OK ? CPL_ERR_AUTHORITY : status;
        goto failed;
    }
    random_capability(journal->action_capability);
    journal->action_initial_completed_steps = chain.state.completed_steps;
    journal->action_completed_steps = chain.state.completed_steps;
    journal->action_effect_completed_steps = 0U;
    journal->action_owner_thread = pthread_self();
    journal->action_held = true;
    journal->action_executed = false;
    journal->action_effect_started = false;
    journal->action_completion_pending = false;
    (void)memset(&journal->action_completion_result, 0,
        sizeof(journal->action_completion_result));
    (void)memcpy(token->capability, journal->action_capability,
        CPL_HASH_SIZE);
    (void)memcpy(token->batch_nonce, batch_nonce, CPL_ID_SIZE);
    token->generation = generation;
    return CPL_OK;

failed:
    unlock_action(journal);
    return status;
}

static int validate_action_token(cpl_journal *journal,
    const struct cpl_action_token *token) {
    if (!journal->action_held || token == NULL ||
        !pthread_equal(journal->action_owner_thread, pthread_self()) ||
        !same_capability(token->capability, journal->action_capability)) {
        return CPL_ERR_BATCH_TOKEN;
    }
    return CPL_OK;
}

static void release_action_token(cpl_journal *journal) {
    journal->action_held = false;
    journal->action_executed = false;
    journal->action_initial_completed_steps = 0U;
    journal->action_completed_steps = 0U;
    journal->action_effect_completed_steps = 0U;
    journal->action_effect_started = false;
    journal->action_completion_pending = false;
    (void)memset(&journal->action_completion_result, 0,
        sizeof(journal->action_completion_result));
    (void)memset(journal->action_capability, 0, CPL_HASH_SIZE);
    (void)memset(&journal->action_owner_thread, 0,
        sizeof(journal->action_owner_thread));
    unlock_action(journal);
}

static int require_certified_batch(cpl_journal *journal,
    const struct cpl_action_token *token, uint64_t deadline,
    struct cpl_certified_head *certified) {
    int status = cpl_journal_certify(journal, deadline, certified);

    if (status != CPL_OK) {
        return status;
    }
    if ((certified->state.kind != CPL_STATE_BATCH_ACTIVE &&
         certified->state.kind != CPL_STATE_RETIRING_BATCH) ||
        certified->state.generation != token->generation ||
        !same_id(certified->state.exact_batch, token->batch_nonce)) {
        return CPL_ERR_AUTHORITY;
    }
    return CPL_OK;
}

int cpl_journal_execute_batch(cpl_journal *journal,
    const struct cpl_action_token *token, int workdir_parent_dirfd,
    uint64_t deadline_ns, uint64_t *completed_steps) {
    struct cpl_certified_head certified;
    uint64_t deadline = effective_deadline(deadline_ns);
    uint32_t index;
    int status = validate_action_token(journal, token);

    if (completed_steps == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    *completed_steps = 0U;
    if (status != CPL_OK || journal->action_executed) {
        return status == CPL_OK ? CPL_ERR_BATCH_TOKEN : status;
    }
    status = validate_workdir_parent_identity(journal, workdir_parent_dirfd);
    if (status != CPL_OK) {
        return status;
    }
    for (index = 0U; index < CPL_MAX_BATCH_DESCRIPTORS; ++index) {
        const struct cpl_batch_descriptor *descriptor;
        uint32_t step;

        status = require_certified_batch(journal, token, deadline, &certified);
        if (status != CPL_OK) {
            return status;
        }
        if (index >= certified.state.descriptor_count) {
            break;
        }
        if (monotonic_ns() >= deadline) {
            return CPL_ERR_LOCK_TIMEOUT;
        }
        descriptor = &certified.state.descriptors[index];
        step = descriptor_step(descriptor->kind);
        if ((journal->action_completed_steps & step) != 0U) {
            continue;
        }
        if ((journal->action_completed_steps & descriptor->required_steps) !=
            descriptor->required_steps) {
            return CPL_ERR_PRECONDITION;
        }
        if (descriptor->kind == CPL_DESCRIPTOR_PROCESS_ABSENT) {
            bool absent;

            status = observed_identity_status(&descriptor->target, &absent);
            if (status != CPL_OK || !absent) {
                return status == CPL_OK ? CPL_ERR_PROCESS_PRESENT : status;
            }
        } else if (descriptor->kind == CPL_DESCRIPTOR_REAP_PROCESS) {
            if ((journal->action_effect_completed_steps & step) == 0U) {
                int wait_status;
                pid_t reaped;

                journal->action_effect_started = true;
                reaped = waitpid((pid_t)descriptor->target.pid,
                    &wait_status, WNOHANG);
                if (reaped != (pid_t)descriptor->target.pid) {
                    return CPL_ERR_REAP_REQUIRED;
                }
                journal->action_effect_completed_steps |= step;
#ifdef CPL_ENABLE_FAULT_INJECTION
                if (journal->fail_batch_after_effect == step) {
                    journal->fail_batch_after_effect = 0U;
                    return CPL_ERR_SYSTEM;
                }
#endif
            }
        } else if (descriptor->kind == CPL_DESCRIPTOR_REMOVE_WORKDIR) {
            struct stat workdir;
            bool already_absent = false;

            if (certified.state.workdir_bound == 0U) {
                return CPL_ERR_IDENTITY_DRIFT;
            }
            if (fstatat(workdir_parent_dirfd, journal->workdir_name, &workdir,
                    AT_SYMLINK_NOFOLLOW) < 0) {
                if (errno != ENOENT) {
                    return CPL_ERR_IDENTITY_DRIFT;
                }
                already_absent = true;
            } else if (!S_ISDIR(workdir.st_mode) ||
                workdir.st_uid != geteuid() ||
                (workdir.st_mode & (mode_t)0777) != (mode_t)0700 ||
                (uint64_t)workdir.st_dev != certified.state.workdir_dev ||
                (uint64_t)workdir.st_ino != certified.state.workdir_ino) {
                return CPL_ERR_IDENTITY_DRIFT;
            }
            if (!already_absent) {
                if (monotonic_ns() >= deadline) {
                    return CPL_ERR_LOCK_TIMEOUT;
                }
                journal->action_effect_started = true;
                if (unlinkat(workdir_parent_dirfd, journal->workdir_name,
                        AT_REMOVEDIR) < 0) {
                    return errno == ENOTEMPTY ? CPL_ERR_WORKDIR_PRESENT :
                        CPL_ERR_SYSTEM;
                }
#ifdef CPL_ENABLE_FAULT_INJECTION
                if (journal->fail_batch_after_effect == step) {
                    journal->fail_batch_after_effect = 0U;
                    return CPL_ERR_SYSTEM;
                }
#endif
            }
            if (monotonic_ns() >= deadline) {
                return CPL_ERR_LOCK_TIMEOUT;
            }
            journal->action_effect_started = true;
#ifdef CPL_ENABLE_FAULT_INJECTION
            if (journal->fail_workdir_parent_fsync) {
                journal->fail_workdir_parent_fsync = false;
                return CPL_ERR_SYSTEM;
            }
#endif
            if (fsync(workdir_parent_dirfd) < 0) {
                return CPL_ERR_SYSTEM;
            }
        } else if (descriptor->kind == CPL_DESCRIPTOR_TERMINAL_CHECKS) {
            struct stat workdir;

            if (fstatat(workdir_parent_dirfd, journal->workdir_name,
                    &workdir, AT_SYMLINK_NOFOLLOW) == 0 || errno != ENOENT) {
                return CPL_ERR_WORKDIR_PRESENT;
            }
        } else {
            return CPL_ERR_PRECONDITION;
        }
        journal->action_completed_steps |= step;
#ifdef CPL_ENABLE_FAULT_INJECTION
        if (journal->fail_batch_after_step == step) {
            journal->fail_batch_after_step = 0U;
            return CPL_ERR_SYSTEM;
        }
#endif
    }
    journal->action_executed = true;
    *completed_steps = journal->action_completed_steps;
    return CPL_OK;
}

int cpl_journal_complete_batch(cpl_journal *journal,
    const struct cpl_action_token *token, uint64_t deadline_ns,
    uint32_t *token_state, struct cpl_append_result *out) {
    struct cpl_certified_head certified;
    struct cpl_record record;
    uint64_t deadline = effective_deadline(deadline_ns);
    uint32_t record_class = CPL_RECORD_NORMAL;
    int status;

    if (token_state == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    *token_state = CPL_ACTION_TOKEN_RETAINED;
    if (out == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(out, 0, sizeof(*out));
    status = validate_action_token(journal, token);
    if (status != CPL_OK || !journal->action_executed) {
        return status == CPL_OK ? CPL_ERR_BATCH_TOKEN : status;
    }
    if (journal->action_completion_pending) {
        status = cpl_journal_certify(journal, deadline, &certified);
        if (status != CPL_OK) {
            return status;
        }
        if (certified.head_sequence !=
                journal->action_completion_result.sequence ||
            memcmp(certified.head_hash,
                journal->action_completion_result.hash,
                CPL_HASH_SIZE) != 0) {
            return CPL_ERR_AUTHORITY;
        }
        *out = journal->action_completion_result;
        release_action_token(journal);
        *token_state = CPL_ACTION_TOKEN_CONSUMED;
        return CPL_OK;
    }
    status = require_certified_batch(journal, token, deadline, &certified);
    if (status != CPL_OK) {
        return status;
    }
    (void)memset(&record, 0, sizeof(record));
    record.kind = CPL_RECORD_BATCH_DONE;
    record.generation = token->generation;
    record.authority_epoch = certified.state.authority_epoch;
    record.lease_deadline_ns = certified.state.lease_deadline_ns;
    record.completed_steps = journal->action_completed_steps;
    (void)memcpy(record.executor, certified.state.executor, CPL_ID_SIZE);
    (void)memcpy(record.exact_batch, token->batch_nonce, CPL_ID_SIZE);
    if (certified.state.kind == CPL_STATE_RETIRING_BATCH) {
        record.batch_outcome = CPL_BATCH_COMPLETED;
        record_class = CPL_RECORD_RECOVERY;
    } else if (record.authority_epoch != 0U) {
        record_class = CPL_RECORD_RECOVERY;
    }
    status = fault_lifecycle_pause(journal,
        CPL_FAULT_BEFORE_BATCH_COMPLETION_APPEND);
    if (status != CPL_OK) {
        return status;
    }
    status = append_authorized(journal, &record, record_class, deadline, out);
    if (out->sequence != 0U) {
        journal->action_completion_result = *out;
        journal->action_completion_pending = true;
    }
    if (status == CPL_OK) {
        status = fault_lifecycle_pause(journal,
            CPL_FAULT_AFTER_BATCH_COMPLETION_APPEND);
    }
    if (status == CPL_OK) {
        status = certify_exact_result(journal, out, deadline, &certified);
    }
    if (status == CPL_OK) {
        release_action_token(journal);
        *token_state = CPL_ACTION_TOKEN_CONSUMED;
    }
    return status;
}

int cpl_journal_abandon_batch(cpl_journal *journal,
    const struct cpl_action_token *token) {
    int status = ensure_owner(journal);

    if (status != CPL_OK) {
        return status;
    }
    status = validate_action_token(journal, token);
    if (status != CPL_OK) {
        return status;
    }
    if (journal->action_executed || journal->action_effect_started ||
        journal->action_completed_steps !=
            journal->action_initial_completed_steps) {
        return CPL_ERR_PRECONDITION;
    }
    release_action_token(journal);
    return CPL_OK;
}

int cpl_journal_finish_done(cpl_journal *journal, uint64_t generation,
    const uint8_t executor[CPL_ID_SIZE], uint64_t deadline_ns,
    struct cpl_append_result *out) {
    struct cpl_process_identity expected;
    struct cpl_process_identity caller;
    struct cpl_certified_head certified;
    struct cpl_chain chain;
    struct cpl_record record;
    uint64_t deadline = effective_deadline(deadline_ns);
    int status;

    if (executor == NULL || out == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    status = lock_action(journal, deadline);
    if (status != CPL_OK) {
        return status;
    }
    status = cpl_journal_scan(journal, &chain);
    if (status != CPL_OK || chain.state.kind != CPL_STATE_ACTIVE_READY ||
        chain.state.generation != generation ||
        !same_id(chain.state.executor, executor) ||
        chain.state.completed_steps != CPL_ALL_COMPLETED_STEPS) {
        status = status == CPL_OK ? CPL_ERR_AUTHORITY : status;
        goto done;
    }
    identity_from_state(&chain.state, &expected);
    status = cpl_process_observe((int64_t)getpid(), &caller);
    if (status != CPL_OK || !same_process_identity(&expected, &caller)) {
        status = CPL_ERR_PROCESS_IDENTITY;
        goto done;
    }
    (void)memset(&record, 0, sizeof(record));
    record.kind = CPL_RECORD_DONE;
    record.generation = generation;
    record.authority_epoch = chain.state.authority_epoch;
    (void)memcpy(record.executor, executor, CPL_ID_SIZE);
    status = append_authorized(journal, &record,
        record.authority_epoch == 0U ? CPL_RECORD_NORMAL :
            CPL_RECORD_RECOVERY,
        deadline, out);
    if (status == CPL_OK) {
        status = certify_exact_result(journal, out, deadline, &certified);
    }

done:
    unlock_action(journal);
    return status;
}

int cpl_journal_retire_executor(cpl_journal *journal,
    const uint8_t authority[CPL_ID_SIZE], uint64_t authority_epoch,
    uint64_t authority_deadline_ns, uint64_t deadline_ns,
    struct cpl_append_result *out) {
    struct cpl_chain chain;
    struct cpl_record record;
    uint64_t deadline = effective_deadline(deadline_ns);
    uint32_t record_class;
    int status;
    bool action_locked = false;

    if (authority == NULL || !has_id(authority) || out == NULL ||
        authority_epoch != CPL_INITIAL_AUTHORITY_EPOCH ||
        authority_deadline_ns <= monotonic_ns()) {
        return authority_epoch != CPL_INITIAL_AUTHORITY_EPOCH ?
            CPL_ERR_AUTHORITY : CPL_ERR_INVALID_ARGUMENT;
    }
    for (;;) {
        struct cpl_chain rechecked;
        uint64_t now;

        status = cpl_journal_scan(journal, &chain);
        if (status != CPL_OK) {
            return status;
        }
        if (chain.state.kind != CPL_STATE_PREPARED &&
            chain.state.kind != CPL_STATE_ACTIVE_READY &&
            chain.state.kind != CPL_STATE_BATCH_ACTIVE) {
            return CPL_ERR_AUTHORITY;
        }
        status = fault_lifecycle_pause(journal,
            CPL_FAULT_BEFORE_RETIREMENT_EXPIRY_CHECK);
        if (status != CPL_OK) {
            return status;
        }
        now = monotonic_ns();
        if ((chain.state.kind == CPL_STATE_PREPARED &&
             chain.state.deadline_ns > now) ||
            ((chain.state.kind == CPL_STATE_ACTIVE_READY ||
              chain.state.kind == CPL_STATE_BATCH_ACTIVE) &&
             chain.state.lease_deadline_ns > now)) {
            return CPL_ERR_AUTHORITY;
        }
        if (chain.state.kind != CPL_STATE_BATCH_ACTIVE) {
            status = lock_action(journal, deadline);
            if (status != CPL_OK) {
                return status;
            }
            action_locked = true;
        }
        status = fault_lifecycle_pause(journal,
            CPL_FAULT_BEFORE_RETIREMENT_APPEND);
        if (status == CPL_OK) {
            status = cpl_journal_scan(journal, &rechecked);
        }
        if (status != CPL_OK) {
            if (action_locked) {
                unlock_action(journal);
            }
            return status;
        }
        if (memcmp(chain.head_hash, rechecked.head_hash, CPL_HASH_SIZE) != 0) {
            if (action_locked) {
                unlock_action(journal);
                action_locked = false;
            }
            continue;
        }
        now = monotonic_ns();
        if ((rechecked.state.kind == CPL_STATE_PREPARED &&
             rechecked.state.deadline_ns > now) ||
            ((rechecked.state.kind == CPL_STATE_ACTIVE_READY ||
              rechecked.state.kind == CPL_STATE_BATCH_ACTIVE) &&
             rechecked.state.lease_deadline_ns > now)) {
            if (action_locked) {
                unlock_action(journal);
            }
            return CPL_ERR_AUTHORITY;
        }
        chain = rechecked;
        break;
    }
    (void)memset(&record, 0, sizeof(record));
    record.generation = chain.state.generation;
    record.authority_epoch = authority_epoch;
    record.deadline_ns = authority_deadline_ns;
    (void)memcpy(record.authority, authority, CPL_ID_SIZE);
    if (chain.state.kind == CPL_STATE_BATCH_ACTIVE) {
        record.kind = CPL_RECORD_RETIRING_BATCH;
        record_class = CPL_RECORD_RECOVERY;
        (void)memcpy(record.prior_actor, chain.state.executor, CPL_ID_SIZE);
        (void)memcpy(record.exact_batch, chain.state.exact_batch, CPL_ID_SIZE);
    } else {
        record.kind = CPL_RECORD_RETIRING_IDLE;
        record_class = CPL_RECORD_RECOVERY;
        if (chain.state.kind == CPL_STATE_PREPARED) {
            (void)memcpy(record.prior_actor, chain.state.candidate, CPL_ID_SIZE);
        } else {
            (void)memcpy(record.prior_actor, chain.state.executor, CPL_ID_SIZE);
        }
    }
    status = monotonic_ns() >= deadline ? CPL_ERR_LOCK_TIMEOUT :
        append_authorized(journal, &record, record_class, deadline, out);
    if (action_locked) {
        unlock_action(journal);
    }
    return status;
}

int cpl_journal_replace_retirement_authority(cpl_journal *journal,
    const uint8_t authority[CPL_ID_SIZE], uint64_t authority_epoch,
    uint64_t authority_deadline_ns, uint64_t deadline_ns,
    struct cpl_append_result *out) {
    struct cpl_chain chain;
    struct cpl_record record;
    uint64_t deadline = effective_deadline(deadline_ns);
    int status;

    if (authority == NULL || !has_id(authority) || out == NULL ||
        authority_epoch != CPL_MAX_AUTHORITY_EPOCH ||
        authority_deadline_ns <= monotonic_ns()) {
        return authority_epoch != CPL_MAX_AUTHORITY_EPOCH ?
            CPL_ERR_AUTHORITY : CPL_ERR_INVALID_ARGUMENT;
    }
    status = lock_action(journal, deadline);
    if (status != CPL_OK) {
        return status;
    }
    status = cpl_journal_scan(journal, &chain);
    if (status != CPL_OK ||
        (chain.state.kind != CPL_STATE_RETIRING_IDLE &&
         chain.state.kind != CPL_STATE_RETIRING_BATCH) ||
        chain.state.deadline_ns > monotonic_ns() ||
        chain.state.authority_epoch != CPL_INITIAL_AUTHORITY_EPOCH ||
        authority_epoch != CPL_MAX_AUTHORITY_EPOCH) {
        status = status == CPL_OK ? CPL_ERR_AUTHORITY : status;
        goto done;
    }
    status = fault_lifecycle_pause(journal,
        CPL_FAULT_BEFORE_AUTHORITY_REPLACEMENT_APPEND);
    if (status != CPL_OK) {
        goto done;
    }
    (void)memset(&record, 0, sizeof(record));
    record.kind = CPL_RECORD_REPLACE_AUTHORITY;
    record.authority_epoch = authority_epoch;
    record.deadline_ns = authority_deadline_ns;
    (void)memcpy(record.authority, authority, CPL_ID_SIZE);
    status = monotonic_ns() >= deadline ? CPL_ERR_LOCK_TIMEOUT :
        append_authorized(journal, &record, CPL_RECORD_RECOVERY, deadline,
            out);

done:
    unlock_action(journal);
    return status;
}

static void fill_reap_proof(const cpl_journal *journal,
    struct cpl_reap_proof *proof) {
    (void)memcpy(proof->allocation_nonce, journal->nonce, CPL_HASH_SIZE);
    (void)memcpy(proof->certified_hash, journal->reap_head_hash,
        CPL_HASH_SIZE);
    (void)memcpy(proof->capability, journal->reap_capability, CPL_HASH_SIZE);
    proof->generation = journal->reaped_generation;
    proof->authority_epoch = journal->reaped_authority_epoch;
    proof->identity = journal->reaped_identity;
}

int cpl_journal_confirm_executor_reaped(cpl_journal *journal,
    uint64_t deadline_ns, struct cpl_reap_proof *proof) {
    struct cpl_certified_head certified;
    struct cpl_process_identity expected;
    uint64_t deadline = effective_deadline(deadline_ns);
    int wait_status;
    int status;
    pid_t reaped;

    if (proof == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(proof, 0, sizeof(*proof));
    status = lock_action(journal, deadline);
    if (status != CPL_OK) {
        return status;
    }
    status = cpl_journal_certify(journal, deadline, &certified);
    if (status != CPL_OK) {
        goto done;
    }
    identity_from_state(&certified.state, &expected);
    if (!complete_identity(&expected)) {
        status = CPL_ERR_PROCESS_IDENTITY;
        goto done;
    }
    if (journal->reap_proof_valid) {
        if (!same_process_identity(&expected, &journal->reaped_identity) ||
            certified.state.generation != journal->reaped_generation ||
            certified.state.authority_epoch !=
                journal->reaped_authority_epoch ||
            is_zero(journal->reap_capability, CPL_HASH_SIZE) ||
            is_zero(journal->reap_head_hash, CPL_HASH_SIZE)) {
            status = CPL_ERR_AUTHORITY;
            goto done;
        }
        fill_reap_proof(journal, proof);
        status = CPL_OK;
        goto done;
    }
    if (certified.state.kind != CPL_STATE_DONE &&
        certified.state.kind != CPL_STATE_RETIRING_IDLE &&
        certified.state.kind != CPL_STATE_RETIRING_BATCH) {
        status = CPL_ERR_AUTHORITY;
        goto done;
    }
    for (;;) {
        reaped = waitpid((pid_t)expected.pid, &wait_status, WNOHANG);
        if (reaped == (pid_t)expected.pid) {
            break;
        }
        if (reaped < 0) {
            status = CPL_ERR_REAP_REQUIRED;
            goto done;
        }
        if (monotonic_ns() >= deadline) {
            status = CPL_ERR_REAP_REQUIRED;
            goto done;
        }
        retry_pause();
    }
    random_capability(journal->reap_capability);
    journal->reap_proof_valid = true;
    journal->reaped_generation = certified.state.generation;
    journal->reaped_authority_epoch = certified.state.authority_epoch;
    journal->reaped_identity = expected;
    (void)memcpy(journal->reap_head_hash, certified.head_hash, CPL_HASH_SIZE);
    fill_reap_proof(journal, proof);
    status = CPL_OK;

done:
    unlock_action(journal);
    return status;
}

int cpl_journal_recover_executor_reap_proof(cpl_journal *journal,
    uint64_t deadline_ns, struct cpl_reap_proof *proof) {
    struct cpl_certified_head certified;
    uint64_t deadline = effective_deadline(deadline_ns);
    int status;

    if (proof == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(proof, 0, sizeof(*proof));
    status = lock_action(journal, deadline);
    if (status != CPL_OK) {
        return status;
    }
    status = cpl_journal_certify(journal, deadline, &certified);
    if (status != CPL_OK) {
        goto recover_done;
    }
    if (!journal->reap_proof_valid ||
        journal->reaped_generation == 0U ||
        journal->reaped_authority_epoch == 0U ||
        !complete_identity(&journal->reaped_identity) ||
        is_zero(journal->reap_capability, CPL_HASH_SIZE) ||
        is_zero(journal->reap_head_hash, CPL_HASH_SIZE)) {
        status = CPL_ERR_REAP_REQUIRED;
        goto recover_done;
    }
    fill_reap_proof(journal, proof);
    status = CPL_OK;

recover_done:
    unlock_action(journal);
    return status;
}

int cpl_journal_make_delete_authority(cpl_journal *journal, uint32_t kind,
    uint64_t deadline_ns, struct cpl_delete_authority *authority) {
    struct cpl_certified_head certified;
    int status;

    if (authority == NULL || kind != CPL_DELETE_CERTIFIED_DONE) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(authority, 0, sizeof(*authority));
    status = cpl_journal_certify(journal, deadline_ns, &certified);
    if (status != CPL_OK) {
        return status;
    }
    if (!certified.done_authority ||
        is_zero(journal->reap_capability, CPL_HASH_SIZE)) {
        return CPL_ERR_REAP_REQUIRED;
    }
    authority->kind = CPL_DELETE_CERTIFIED_DONE;
    (void)memcpy(authority->allocation_nonce, journal->nonce, CPL_HASH_SIZE);
    (void)memcpy(authority->certified_hash, certified.head_hash,
        CPL_HASH_SIZE);
    (void)memcpy(authority->capability, journal->reap_capability,
        CPL_HASH_SIZE);
    return CPL_OK;
}

static bool valid_reap_proof(cpl_journal *journal,
    const struct cpl_reap_proof *proof, const struct cpl_chain *chain) {
    struct cpl_process_identity chain_identity;

    if (proof == NULL || chain == NULL) {
        return false;
    }
    identity_from_state(&chain->state, &chain_identity);
    return journal->reap_proof_valid &&
        memcmp(proof->allocation_nonce, journal->nonce,
            CPL_HASH_SIZE) == 0 &&
        proof->generation == journal->reaped_generation &&
        proof->authority_epoch == journal->reaped_authority_epoch &&
        same_process_identity(&proof->identity,
            &journal->reaped_identity) &&
        same_capability(proof->capability, journal->reap_capability) &&
        memcmp(proof->certified_hash, journal->reap_head_hash,
            CPL_HASH_SIZE) == 0 &&
        chain->state.generation == journal->reaped_generation &&
        chain->state.authority_epoch == journal->reaped_authority_epoch &&
        same_process_identity(&chain_identity, &journal->reaped_identity);
}

int cpl_journal_reconcile_interrupted_batch(cpl_journal *journal,
    const struct cpl_reap_proof *proof, uint64_t deadline_ns,
    struct cpl_append_result *out) {
    struct cpl_chain chain;
    struct cpl_record record;
    uint64_t deadline = effective_deadline(deadline_ns);
    int status = cpl_journal_scan(journal, &chain);

    if (out == NULL || status != CPL_OK ||
        chain.state.kind != CPL_STATE_RETIRING_BATCH ||
        !valid_reap_proof(journal, proof, &chain)) {
        return status == CPL_OK ? CPL_ERR_AUTHORITY : status;
    }
    status = lock_action(journal, deadline);
    if (status != CPL_OK) {
        return status;
    }
    (void)memset(&record, 0, sizeof(record));
    record.kind = CPL_RECORD_BATCH_DONE;
    record.generation = chain.state.generation;
    record.authority_epoch = chain.state.authority_epoch;
    record.lease_deadline_ns = chain.state.lease_deadline_ns;
    record.completed_steps = chain.state.completed_steps;
    record.batch_outcome = CPL_BATCH_INTERRUPTED;
    (void)memcpy(record.executor, chain.state.executor, CPL_ID_SIZE);
    (void)memcpy(record.exact_batch, chain.state.exact_batch, CPL_ID_SIZE);
    status = append_authorized(journal, &record, CPL_RECORD_RECOVERY, deadline,
        out);
    unlock_action(journal);
    return status;
}

int cpl_journal_prepare_successor(cpl_journal *journal,
    const struct cpl_reap_proof *proof, uint64_t generation,
    const uint8_t candidate[CPL_ID_SIZE], uint64_t claim_deadline_ns,
    uint64_t deadline_ns, struct cpl_append_result *out) {
    struct cpl_chain chain;
    struct cpl_record record;
    uint64_t deadline = effective_deadline(deadline_ns);
    int status = cpl_journal_scan(journal, &chain);

    if (out == NULL || candidate == NULL || !has_id(candidate) ||
        claim_deadline_ns <= monotonic_ns() || status != CPL_OK ||
        chain.state.kind != CPL_STATE_RETIRING_IDLE ||
        generation != chain.state.generation + 1U ||
        !valid_reap_proof(journal, proof, &chain)) {
        return status == CPL_OK ? CPL_ERR_AUTHORITY : status;
    }
    status = lock_action(journal, deadline);
    if (status != CPL_OK) {
        return status;
    }
    status = fault_lifecycle_pause(journal,
        CPL_FAULT_BEFORE_SUCCESSOR_APPEND);
    if (status != CPL_OK) {
        unlock_action(journal);
        return status;
    }
    (void)memset(&record, 0, sizeof(record));
    record.kind = CPL_RECORD_PREPARED;
    record.generation = generation;
    record.authority_epoch = chain.state.authority_epoch;
    record.deadline_ns = claim_deadline_ns;
    (void)memcpy(record.candidate, candidate, CPL_ID_SIZE);
    status = append_authorized(journal, &record, CPL_RECORD_RECOVERY, deadline,
        out);
    unlock_action(journal);
    return status;
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
    random_capability(receipt->capability);
#ifdef CPL_ENABLE_FAULT_INJECTION
    if (pthread_mutex_lock(&receipt_mutex) == 0) {
        size_t index;

        for (index = 0U; index < CPL_RECEIPT_REGISTRY_CAPACITY; ++index) {
            if (!delete_receipt_used[index]) {
                (void)memcpy(delete_receipts[index], receipt->capability,
                    CPL_HASH_SIZE);
                delete_receipt_used[index] = true;
                break;
            }
        }
        (void)pthread_mutex_unlock(&receipt_mutex);
    }
#endif
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
    status = validate_requested_workdir(journal, workdir_parent_dirfd,
        workdir_name);
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
        bool execution_absent = false;

        if (!journal->reap_proof_valid ||
            certified.state.workdir_bound == 0U) {
            status = CPL_ERR_REAP_REQUIRED;
            goto done;
        }
        status = observed_identity_status(&journal->reaped_identity,
            &execution_absent);
        if (status != CPL_OK || !execution_absent) {
            status = status == CPL_OK ? CPL_ERR_PROCESS_PRESENT : status;
            goto done;
        }
        if (monotonic_ns() >= deadline) {
            status = CPL_ERR_LOCK_TIMEOUT;
            goto done;
        }
        fault_exit("after_process_absence_verified");
    } else if (certified.has_intent &&
        certified.state.no_dependent_artifact == 0U) {
        status = CPL_ERR_AUTHORITY;
        goto done;
    }
    status = require_workdir_absent(workdir_parent_dirfd,
        journal->workdir_name);
    if (status != CPL_OK) {
        goto done;
    }
    fault_exit("after_workdir_absence_verified");
    status = validate_open_journal_identity(journal, parent_dirfd,
        journal_name, false);
    if (status != CPL_OK) {
        goto done;
    }
    if (monotonic_ns() >= deadline) {
        status = CPL_ERR_LOCK_TIMEOUT;
        goto done;
    }
    if (unlinkat(parent_dirfd, journal_name, 0) < 0) {
        status = CPL_ERR_SYSTEM;
        goto done;
    }
    fault_exit("after_journal_unlinkat");
    status = validate_parent_identity(journal, parent_dirfd);
    if (status != CPL_OK || monotonic_ns() >= deadline ||
        fsync(parent_dirfd) < 0) {
        if (status == CPL_OK) {
            status = monotonic_ns() >= deadline ? CPL_ERR_LOCK_TIMEOUT :
                CPL_ERR_SYSTEM;
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
    status = validate_requested_workdir(journal, workdir_parent_dirfd,
        workdir_name);
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
    status = require_workdir_absent(workdir_parent_dirfd,
        journal->workdir_name);
    if (status != CPL_OK) {
        goto done;
    }
    status = validate_open_journal_identity(journal, parent_dirfd,
        journal_name, true);
    if (status != CPL_OK) {
        goto done;
    }
    status = validate_parent_identity(journal, parent_dirfd);
    if (status != CPL_OK || monotonic_ns() >= deadline ||
        fsync(parent_dirfd) < 0) {
        if (status == CPL_OK) {
            status = monotonic_ns() >= deadline ? CPL_ERR_LOCK_TIMEOUT :
                CPL_ERR_SYSTEM;
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
    if (journal->action_held && !journal->fork_invalid) {
        return;
    }
    unregister_handle(journal);
    if (journal->fd >= 0) {
        (void)close(journal->fd);
    }
    if (journal->append_lock_fd >= 0) {
        (void)close(journal->append_lock_fd);
    }
    if (journal->action_lock_fd >= 0) {
        (void)close(journal->action_lock_fd);
    }
    if (!journal->fork_invalid) {
        (void)pthread_mutex_destroy(&journal->append_mutex);
        (void)pthread_mutex_destroy(&journal->action_mutex);
    }
    (void)memset(journal, 0, sizeof(*journal));
    free(journal);
}

#ifdef CPL_ENABLE_FAULT_INJECTION
int cpl_fault_validate_reap_proof(cpl_journal *journal,
    const struct cpl_reap_proof *proof, const struct cpl_state *state) {
    struct cpl_chain chain;
    int status = ensure_owner(journal);

    if (status != CPL_OK || proof == NULL || state == NULL) {
        return status == CPL_OK ? CPL_ERR_INVALID_ARGUMENT : status;
    }
    (void)memset(&chain, 0, sizeof(chain));
    chain.state = *state;
    return valid_reap_proof(journal, proof, &chain) ? CPL_OK :
        CPL_ERR_AUTHORITY;
}

int cpl_fault_configure_create_pause(uint32_t point, int notify_fd,
    int wait_fd, uint64_t deadline_ns) {
    if (point < CPL_FAULT_BEFORE_CREATE_OPENAT ||
        point > CPL_FAULT_BEFORE_CREATE_PARENT_FSYNC || notify_fd < 0 ||
        wait_fd < 0 || deadline_ns <= monotonic_ns()) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    create_pause_point = point;
    create_pause_notify_fd = notify_fd;
    create_pause_wait_fd = wait_fd;
    create_pause_deadline_ns = deadline_ns;
    return CPL_OK;
}

int cpl_fault_configure_lifecycle_pause(cpl_journal *journal, uint32_t point,
    int notify_fd, int wait_fd) {
    int status = ensure_owner(journal);

    if (status != CPL_OK || point < CPL_FAULT_BEFORE_WORKDIR_BOUND_APPEND ||
        point > CPL_FAULT_AFTER_BATCH_COMPLETION_APPEND ||
        notify_fd < 0 ||
        wait_fd < 0) {
        return status == CPL_OK ? CPL_ERR_INVALID_ARGUMENT : status;
    }
    journal->pause_point = point;
    journal->pause_notify_fd = notify_fd;
    journal->pause_wait_fd = wait_fd;
    return CPL_OK;
}

int cpl_fault_fail_batch_after_step(cpl_journal *journal, uint32_t step) {
    int status = ensure_owner(journal);

    if (status != CPL_OK ||
        (step != CPL_STEP_PROCESS_ABSENT &&
         step != CPL_STEP_EXECUTOR_REAPED &&
         step != CPL_STEP_WORKDIR_REMOVED &&
         step != CPL_STEP_TERMINAL_CHECKS)) {
        return status == CPL_OK ? CPL_ERR_INVALID_ARGUMENT : status;
    }
    journal->fail_batch_after_step = step;
    return CPL_OK;
}

int cpl_fault_fail_batch_after_effect(cpl_journal *journal, uint32_t step) {
    int status = ensure_owner(journal);

    if (status != CPL_OK ||
        (step != CPL_STEP_EXECUTOR_REAPED &&
         step != CPL_STEP_WORKDIR_REMOVED)) {
        return status == CPL_OK ? CPL_ERR_INVALID_ARGUMENT : status;
    }
    journal->fail_batch_after_effect = step;
    return CPL_OK;
}

int cpl_fault_fail_next_workdir_parent_fsync(cpl_journal *journal) {
    int status = ensure_owner(journal);

    if (status != CPL_OK) {
        return status;
    }
    journal->fail_workdir_parent_fsync = true;
    return CPL_OK;
}

int cpl_fault_force_atfork_registration_failure(bool enabled) {
    force_atfork_registration_failure = enabled;
    return CPL_OK;
}

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

int cpl_fault_hold_action_lock(cpl_journal *journal, int notify_fd, int wait_fd,
    uint64_t deadline_ns) {
    uint64_t deadline = effective_deadline(deadline_ns);
    int status = ensure_owner(journal);

    if (status != CPL_OK || notify_fd < 0 || wait_fd < 0) {
        return status == CPL_OK ? CPL_ERR_INVALID_ARGUMENT : status;
    }
    status = lock_action(journal, deadline);
    if (status != CPL_OK) {
        return status;
    }
    status = checked_byte_write(notify_fd);
    if (status == CPL_OK) {
        status = checked_byte_read(wait_fd);
    }
    unlock_action(journal);
    return status;
}

int cpl_fault_probe_action_lock(cpl_journal *journal, uint64_t deadline_ns) {
    int status = ensure_owner(journal);

    if (status != CPL_OK) {
        return status;
    }
    status = lock_action(journal, effective_deadline(deadline_ns));
    if (status == CPL_OK) {
        unlock_action(journal);
    }
    return status;
}

int cpl_fault_append_bytes(cpl_journal *journal, const uint8_t *bytes,
    uint32_t length) {
    uint64_t deadline = effective_deadline(0U);
    ssize_t written;
    int status = ensure_owner(journal);

    if (status != CPL_OK || bytes == NULL || length == 0U) {
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
    written = write(journal->fd, bytes, length);
    (void)release_flock(journal->append_lock_fd);
    (void)pthread_mutex_unlock(&journal->append_mutex);
    return written == (ssize_t)length ? CPL_OK : CPL_ERR_IO_SHORT;
}

int cpl_fault_encode_record(cpl_journal *journal, const uint8_t *record_bytes,
    uint32_t record_len, uint8_t *out, uint32_t capacity,
    uint32_t *out_len) {
    struct cpl_chain chain;
    struct cpl_record record;
    uint8_t hash[CPL_HASH_SIZE];
    size_t encoded_length = 0U;
    int status = ensure_owner(journal);

    if (status != CPL_OK || record_bytes == NULL || out == NULL ||
        out_len == NULL || record_len != sizeof(record)) {
        return status == CPL_OK ? CPL_ERR_INVALID_ARGUMENT : status;
    }
    status = cpl_journal_scan(journal, &chain);
    if (status != CPL_OK) {
        return status;
    }
    (void)memcpy(&record, record_bytes, sizeof(record));
    if (is_zero(record.parent_hash, CPL_HASH_SIZE)) {
        (void)memcpy(record.parent_hash, chain.head_hash, CPL_HASH_SIZE);
    }
    status = encode_record(journal, &record, chain.head_sequence + 1U,
        record.parent_hash, out, capacity, &encoded_length, hash);
    if (status != CPL_OK) {
        return status;
    }
    *out_len = (uint32_t)encoded_length;
    return CPL_OK;
}

int cpl_fault_inject_header_mismatch(cpl_journal *journal,
    const uint8_t *record_bytes, uint32_t record_len, uint32_t mismatch) {
    uint8_t encoded[CPL_HEADER_SIZE + sizeof(struct cpl_record)];
    uint32_t encoded_length = 0U;
    uint32_t checksum;
    int status;

    if (mismatch < 1U || mismatch > 3U) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    status = cpl_fault_encode_record(journal, record_bytes, record_len,
        encoded, (uint32_t)sizeof(encoded), &encoded_length);
    if (status != CPL_OK) {
        return status;
    }
    if (mismatch == 1U) {
        put_u64(encoded + 56U, get_u64(encoded + 56U) + 1U);
    } else if (mismatch == 2U) {
        put_u32(encoded + 96U, get_u32(encoded + 96U) + 1U);
    } else {
        encoded[64U] ^= 1U;
    }
    put_u32(encoded + 104U, 0U);
    checksum = crc32c(encoded, encoded_length);
    put_u32(encoded + 104U, checksum);
    return cpl_fault_append_bytes(journal, encoded, encoded_length);
}

static int fault_create_marker(int parent_dirfd, const char *name) {
    struct stat parent_stat;
    int fd;
    int status = validate_component(name);

    if (status != CPL_OK) {
        return status;
    }
    status = validate_parent(parent_dirfd, &parent_stat);
    if (status != CPL_OK) {
        return status;
    }
    fd = openat(parent_dirfd, name,
        O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, (mode_t)0600);
    if (fd < 0) {
        return errno == EEXIST ? CPL_ERR_EXISTS : CPL_ERR_SYSTEM;
    }
    if (close(fd) < 0 || fsync(parent_dirfd) < 0) {
        return CPL_ERR_SYSTEM;
    }
    return CPL_OK;
}

int cpl_fault_try_bound_gate(cpl_journal *journal, uint32_t gate_kind,
    const struct cpl_workdir_receipt *receipt, int marker_parent_dirfd,
    const char *marker_name) {
    struct cpl_chain chain;
    struct stat workdir_stat;
    int status = ensure_owner(journal);

    if (status != CPL_OK) {
        return status;
    }
    if ((gate_kind != 1U && gate_kind != 2U) || receipt == NULL ||
        receipt->state != CPL_WORKDIR_PARENT_DIRSYNCED ||
        !same_capability(receipt->capability, journal->workdir_capability)) {
        return CPL_ERR_RECEIPT;
    }
    status = validate_workdir_parent_identity(journal, marker_parent_dirfd);
    if (status != CPL_OK) {
        return CPL_ERR_RECEIPT;
    }
    if (fstatat(marker_parent_dirfd, journal->workdir_name, &workdir_stat,
            AT_SYMLINK_NOFOLLOW) < 0 || !S_ISDIR(workdir_stat.st_mode) ||
        (uint64_t)workdir_stat.st_dev != receipt->workdir_dev ||
        (uint64_t)workdir_stat.st_ino != receipt->workdir_ino) {
        return CPL_ERR_RECEIPT;
    }
    status = cpl_journal_scan(journal, &chain);
    if (status != CPL_OK) {
        return status;
    }
    if (!chain.state.workdir_bound || chain.state.workdir_dev != receipt->workdir_dev ||
        chain.state.workdir_ino != receipt->workdir_ino) {
        return CPL_ERR_RECEIPT;
    }
    return fault_create_marker(marker_parent_dirfd, marker_name);
}

int cpl_fault_try_cleanup_gate(const struct cpl_delete_receipt *receipt,
    int marker_parent_dirfd, const char *marker_name) {
    size_t index;
    bool found = false;

    if (receipt == NULL ||
        receipt->state != CPL_JOURNAL_UNLINK_PARENT_DIRSYNCED ||
        !receipt->slot_releasable ||
        is_zero(receipt->capability, CPL_HASH_SIZE)) {
        return CPL_ERR_RECEIPT;
    }
    if (pthread_mutex_lock(&receipt_mutex) != 0) {
        return CPL_ERR_SYSTEM;
    }
    for (index = 0U; index < CPL_RECEIPT_REGISTRY_CAPACITY; ++index) {
        if (delete_receipt_used[index] &&
            memcmp(delete_receipts[index], receipt->capability,
                CPL_HASH_SIZE) == 0) {
            delete_receipt_used[index] = false;
            (void)memset(delete_receipts[index], 0, CPL_HASH_SIZE);
            found = true;
            break;
        }
    }
    (void)pthread_mutex_unlock(&receipt_mutex);
    return found ? fault_create_marker(marker_parent_dirfd, marker_name) :
        CPL_ERR_RECEIPT;
}
#endif
