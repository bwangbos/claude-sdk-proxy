#ifndef CLAUDE_PROXY_LIFECYCLE_H
#define CLAUDE_PROXY_LIFECYCLE_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CPL_HASH_SIZE 32U
#define CPL_ID_SIZE 32U
#define CPL_REASON_SIZE 64U

typedef struct cpl_journal cpl_journal;

enum cpl_error {
    CPL_OK = 0,
    CPL_ERR_INVALID_ARGUMENT = 1,
    CPL_ERR_SYSTEM = 2,
    CPL_ERR_EXISTS = 3,
    CPL_ERR_NOT_FOUND = 4,
    CPL_ERR_SYMLINK = 5,
    CPL_ERR_UNSAFE_FILE = 6,
    CPL_ERR_IDENTITY_DRIFT = 7,
    CPL_ERR_NONCE_MISMATCH = 8,
    CPL_ERR_LOCK_TIMEOUT = 9,
    CPL_ERR_NORMAL_LIMIT = 10,
    CPL_ERR_HARD_LIMIT = 11,
    CPL_ERR_RECORD_CLASS = 12,
    CPL_ERR_PARENT_MISMATCH = 13,
    CPL_ERR_ILLEGAL_TRANSITION = 14,
    CPL_ERR_CORRUPT = 15,
    CPL_ERR_CERTIFY_TIMEOUT = 16,
    CPL_ERR_AUTHORITY = 17,
    CPL_ERR_PROCESS_PRESENT = 18,
    CPL_ERR_WORKDIR_PRESENT = 19,
    CPL_ERR_NOT_ABSENT = 20,
    CPL_ERR_FORK_INHERITED = 21,
    CPL_ERR_CLOSED = 22,
    CPL_ERR_IO_SHORT = 23,
    CPL_ERR_UNSUPPORTED = 24,
};

enum cpl_storage_state {
    CPL_STORAGE_NONE = 0,
    CPL_INTENT_PARENT_DIRSYNCED = 1,
    CPL_JOURNAL_UNLINK_PARENT_DIRSYNCED = 2,
};

enum cpl_delete_authority_kind {
    CPL_DELETE_CERTIFIED_DONE = 1,
    CPL_DELETE_UNRELEASED_PARTIAL_CREATE = 2,
};

enum cpl_record_class {
    CPL_RECORD_NORMAL = 1,
    CPL_RECORD_RECOVERY = 2,
};

enum cpl_state_kind {
    CPL_STATE_NO_GENERATION = 0,
    CPL_STATE_PREPARED = 1,
    CPL_STATE_ACTIVE_READY = 2,
    CPL_STATE_BATCH_ACTIVE = 3,
    CPL_STATE_RETIRING_IDLE = 4,
    CPL_STATE_RETIRING_BATCH = 5,
    CPL_STATE_DONE = 6,
    CPL_STATE_UNCONFIRMED = 7,
};

enum cpl_record_kind {
    CPL_RECORD_INTENT = 1,
    CPL_RECORD_PREPARED = 2,
    CPL_RECORD_ACTIVE_READY = 3,
    CPL_RECORD_BATCH_ACTIVE = 4,
    CPL_RECORD_BATCH_DONE = 5,
    CPL_RECORD_RETIRING_IDLE = 6,
    CPL_RECORD_RETIRING_BATCH = 7,
    CPL_RECORD_REPLACE_AUTHORITY = 8,
    CPL_RECORD_DONE = 9,
    CPL_RECORD_UNCONFIRMED = 10,
};

enum cpl_batch_outcome {
    CPL_BATCH_NONE = 0,
    CPL_BATCH_COMPLETED = 1,
    CPL_BATCH_INTERRUPTED = 2,
};

enum cpl_completed_step {
    CPL_STEP_PROCESS_ABSENT = 1U << 0,
    CPL_STEP_EXECUTOR_REAPED = 1U << 1,
    CPL_STEP_WORKDIR_REMOVED = 1U << 2,
    CPL_STEP_TERMINAL_CHECKS = 1U << 3,
};

#define CPL_ALL_COMPLETED_STEPS                                                \
    (CPL_STEP_PROCESS_ABSENT | CPL_STEP_EXECUTOR_REAPED |                     \
     CPL_STEP_WORKDIR_REMOVED | CPL_STEP_TERMINAL_CHECKS)

struct cpl_state {
    uint32_t kind;
    uint32_t retained_kind;
    uint64_t generation;
    uint64_t cleanup_epoch;
    uint64_t authority_epoch;
    uint64_t deadline_ns;
    uint64_t lease_deadline_ns;
    uint64_t completed_steps;
    int64_t process_pid;
    uint64_t process_start_ns;
    uint32_t process_uid;
    int32_t process_pgid;
    int32_t process_sid;
    uint32_t process_identity_flags;
    uint64_t executable_dev;
    uint64_t executable_ino;
    uint32_t batch_outcome;
    uint32_t reserved;
    uint8_t boot_id[CPL_HASH_SIZE];
    uint8_t executable_hash[CPL_HASH_SIZE];
    uint8_t candidate[CPL_ID_SIZE];
    uint8_t executor[CPL_ID_SIZE];
    uint8_t prior_actor[CPL_ID_SIZE];
    uint8_t authority[CPL_ID_SIZE];
    uint8_t exact_batch[CPL_ID_SIZE];
    uint8_t inherited_batch[CPL_ID_SIZE];
    uint8_t reason[CPL_REASON_SIZE];
};

struct cpl_record {
    uint32_t kind;
    uint32_t batch_outcome;
    uint64_t generation;
    uint64_t cleanup_epoch;
    uint64_t authority_epoch;
    uint64_t deadline_ns;
    uint64_t lease_deadline_ns;
    uint64_t completed_steps;
    int64_t process_pid;
    uint64_t process_start_ns;
    uint32_t process_uid;
    int32_t process_pgid;
    int32_t process_sid;
    uint32_t process_identity_flags;
    uint64_t executable_dev;
    uint64_t executable_ino;
    uint8_t parent_hash[CPL_HASH_SIZE];
    uint8_t boot_id[CPL_HASH_SIZE];
    uint8_t executable_hash[CPL_HASH_SIZE];
    uint8_t candidate[CPL_ID_SIZE];
    uint8_t executor[CPL_ID_SIZE];
    uint8_t prior_actor[CPL_ID_SIZE];
    uint8_t authority[CPL_ID_SIZE];
    uint8_t exact_batch[CPL_ID_SIZE];
    uint8_t inherited_batch[CPL_ID_SIZE];
    uint8_t reason[CPL_REASON_SIZE];
};

struct cpl_chain {
    struct cpl_state state;
    uint8_t head_hash[CPL_HASH_SIZE];
    uint64_t head_sequence;
    uint64_t physical_eof;
    uint64_t canonical_records;
    uint64_t invalid_bytes;
    uint64_t stale_records;
    bool has_intent;
    bool unhealthy;
    uint8_t reserved[6];
};

struct cpl_certified_head {
    struct cpl_state state;
    uint8_t head_hash[CPL_HASH_SIZE];
    uint64_t head_sequence;
    uint64_t physical_eof;
    uint32_t attempts;
    bool has_intent;
    bool done_authority;
    bool partial_create_authority;
    uint8_t reserved;
};

struct cpl_create_receipt {
    enum cpl_storage_state state;
    uint8_t intent_hash[CPL_HASH_SIZE];
};

struct cpl_delete_authority {
    enum cpl_delete_authority_kind kind;
    uint8_t allocation_nonce[CPL_HASH_SIZE];
    uint8_t certified_hash[CPL_HASH_SIZE];
};

struct cpl_delete_receipt {
    enum cpl_storage_state state;
    bool slot_releasable;
};

int cpl_journal_create_at(int parent_dirfd, const char *journal_name,
    const uint8_t nonce[CPL_HASH_SIZE], uint64_t normal_limit,
    uint64_t hard_limit, cpl_journal **out,
    struct cpl_create_receipt *receipt);
int cpl_journal_open_at(int parent_dirfd, const char *journal_name,
    const uint8_t nonce[CPL_HASH_SIZE], uint64_t normal_limit,
    uint64_t hard_limit, cpl_journal **out);
int cpl_journal_append(cpl_journal *j, const uint8_t *record,
    uint32_t record_len, uint32_t record_class, uint64_t deadline_ns,
    uint8_t out_hash[CPL_HASH_SIZE]);
int cpl_journal_scan(cpl_journal *j, struct cpl_chain *out);
int cpl_journal_certify(cpl_journal *j, uint64_t deadline_ns,
    struct cpl_certified_head *out);
int cpl_lifecycle_apply(const struct cpl_state *current,
    const struct cpl_record *record, struct cpl_state *out);
int cpl_journal_delete_at(cpl_journal **inout_j, int parent_dirfd,
    const char *journal_name, int workdir_parent_dirfd,
    const char *workdir_name, const struct cpl_delete_authority *authority,
    uint64_t deadline_ns, struct cpl_delete_receipt *receipt);
int cpl_journal_reconcile_absent_after_crash(cpl_journal **inout_j,
    int parent_dirfd, const char *journal_name, int workdir_parent_dirfd,
    const char *workdir_name, const struct cpl_delete_authority *authority,
    uint64_t deadline_ns, struct cpl_delete_receipt *receipt);
void cpl_journal_close(cpl_journal *j);

#ifdef CPL_ENABLE_FAULT_INJECTION
int cpl_fault_hold_append_lock(cpl_journal *j, int notify_fd, int wait_fd,
    uint64_t deadline_ns);
int cpl_fault_configure_certify_pause(int notify_fd, int wait_fd);
#endif

#ifdef __cplusplus
}
#endif

#endif
