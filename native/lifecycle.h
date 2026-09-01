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
#define CPL_WORKDIR_NAME_SIZE 64U
#define CPL_MAX_BATCH_DESCRIPTORS 4U
#define CPL_PHYSICAL_RECORD_SIZE 1172U
#define CPL_MAX_RECOVERY_CLEANUP_BATCHES CPL_MAX_BATCH_DESCRIPTORS
#define CPL_RECOVERY_HANDOFF_RECORD_COUNT 5U
#define CPL_RECOVERY_TERMINAL_RECORD_COUNT 1U
#define CPL_RECOVERY_RECORD_COUNT                                        \
    (CPL_RECOVERY_HANDOFF_RECORD_COUNT +                                \
     (2U * CPL_MAX_RECOVERY_CLEANUP_BATCHES) +                          \
     CPL_RECOVERY_TERMINAL_RECORD_COUNT)
#define CPL_RECOVERY_BYTES                                                \
    (CPL_PHYSICAL_RECORD_SIZE * CPL_RECOVERY_RECORD_COUNT)
/*
 * The finite worst-case recovery lineage is five handoff records
 * (retirement, one authority replacement, interrupted-batch resolution,
 * successor preparation, successor activation), two records for each of the
 * four unique ordered cleanup steps, then terminal DONE or UNCONFIRMED.
 */
#define CPL_INITIAL_AUTHORITY_EPOCH 1U
#define CPL_MAX_AUTHORITY_REPLACEMENTS 1U
#define CPL_MAX_AUTHORITY_EPOCH                                           \
    (CPL_INITIAL_AUTHORITY_EPOCH + CPL_MAX_AUTHORITY_REPLACEMENTS)

#define CPL_ABI_PROCESS_IDENTITY_SIZE 112U
#define CPL_ABI_BATCH_DESCRIPTOR_SIZE 120U
#define CPL_ABI_STATE_SIZE 1032U
#define CPL_ABI_RECORD_SIZE 1064U
#define CPL_ABI_CHAIN_SIZE 1112U
#define CPL_ABI_CERTIFIED_HEAD_SIZE 1088U
#define CPL_ABI_APPEND_RESULT_SIZE 1072U
#define CPL_ABI_CREATE_RECEIPT_SIZE 68U
#define CPL_ABI_WORKDIR_RECEIPT_SIZE 88U
#define CPL_ABI_DELETE_AUTHORITY_SIZE 100U
#define CPL_ABI_DELETE_RECEIPT_SIZE 40U
#define CPL_ABI_ACTION_TOKEN_SIZE 72U
#define CPL_ABI_REAP_PROOF_SIZE 72U
#define CPL_CONTROL_MAGIC 0x464c5043U
#define CPL_CONTROL_VERSION 1U
#define CPL_CONTROL_MAX_PAYLOAD 4096U
#define CPL_CONTROL_WIRE_HEADER_SIZE 44U
#define CPL_CONTROL_WIRE_CHECKSUM_SIZE 4U
#define CPL_CONTROL_MAX_WIRE_SIZE                                         \
    (CPL_CONTROL_WIRE_HEADER_SIZE + CPL_CONTROL_MAX_PAYLOAD +            \
     CPL_CONTROL_WIRE_CHECKSUM_SIZE)
#define CPL_ABI_CONTROL_FRAME_SIZE 4144U

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
    CPL_ERR_PROCESS_IDENTITY = 25,
    CPL_ERR_ACTION_LOCK = 26,
    CPL_ERR_BATCH_TOKEN = 27,
    CPL_ERR_PRECONDITION = 28,
    CPL_ERR_REAP_REQUIRED = 29,
    CPL_ERR_RECEIPT = 30,
    CPL_ERR_CONTROL_FRAME = 31,
    CPL_ERR_CONTROL_PHASE = 32,
    CPL_ERR_CONTROL_PAYLOAD = 33,
};

enum cpl_control_type {
    CPL_CONTROL_SUPERVISOR_IDENTITY = 1,
    CPL_CONTROL_IDENTITY_ACK = 2,
    CPL_CONTROL_ANCHOR_IDENTITY = 3,
    CPL_CONTROL_ANCHOR_ACK = 4,
    CPL_CONTROL_CLI_ARMED = 5,
    CPL_CONTROL_ARMED_ACK = 6,
    CPL_CONTROL_CLI_RUNNING = 7,
    CPL_CONTROL_CLEANUP_REQUEST = 8,
    CPL_CONTROL_SELF_TERM_REQUEST = 9,
    CPL_CONTROL_ERROR = 10,
};

enum cpl_control_phase {
    CPL_CONTROL_PHASE_NONE = 0,
    CPL_CONTROL_PHASE_SUPERVISOR_IDENTITY = 1,
    CPL_CONTROL_PHASE_IDENTITY_ACK = 2,
    CPL_CONTROL_PHASE_ANCHOR_IDENTITY = 3,
    CPL_CONTROL_PHASE_ANCHOR_ACK = 4,
    CPL_CONTROL_PHASE_CLI_ARMED = 5,
    CPL_CONTROL_PHASE_ARMED_ACK = 6,
    CPL_CONTROL_PHASE_CLI_RUNNING = 7,
    CPL_CONTROL_PHASE_CLEANUP_REQUEST = 8,
    CPL_CONTROL_PHASE_SELF_TERM_REQUEST = 9,
    CPL_CONTROL_PHASE_ERROR = 10,
};

struct cpl_control_frame {
    uint32_t magic;
    uint16_t version;
    uint16_t type;
    uint32_t payload_length;
    uint8_t allocation_nonce[CPL_HASH_SIZE];
    uint8_t payload[CPL_CONTROL_MAX_PAYLOAD];
    uint32_t checksum;
};

enum cpl_storage_state {
    CPL_STORAGE_NONE = 0,
    CPL_INTENT_PARENT_DIRSYNCED = 1,
    CPL_JOURNAL_UNLINK_PARENT_DIRSYNCED = 2,
    CPL_WORKDIR_PARENT_DIRSYNCED = 3,
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
    CPL_RECORD_WORKDIR_BOUND = 11,
    CPL_RECORD_NO_DEPENDENT_ARTIFACT = 12,
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

enum cpl_action_token_state {
    CPL_ACTION_TOKEN_RETAINED = 1,
    CPL_ACTION_TOKEN_CONSUMED = 2,
};

#define CPL_ALL_COMPLETED_STEPS                                                \
    (CPL_STEP_PROCESS_ABSENT | CPL_STEP_EXECUTOR_REAPED |                     \
     CPL_STEP_WORKDIR_REMOVED | CPL_STEP_TERMINAL_CHECKS)

enum cpl_process_identity_flag {
    CPL_ID_BOOT = 1U << 0,
    CPL_ID_START = 1U << 1,
    CPL_ID_UID = 1U << 2,
    CPL_ID_GROUP_SESSION = 1U << 3,
    CPL_ID_EXECUTABLE_INODE = 1U << 4,
    CPL_ID_EXECUTABLE_HASH = 1U << 5,
};

#define CPL_COMPLETE_PROCESS_IDENTITY                                         \
    (CPL_ID_BOOT | CPL_ID_START | CPL_ID_UID | CPL_ID_GROUP_SESSION |         \
     CPL_ID_EXECUTABLE_INODE | CPL_ID_EXECUTABLE_HASH)

enum cpl_batch_descriptor_kind {
    CPL_DESCRIPTOR_PROCESS_ABSENT = 1,
    CPL_DESCRIPTOR_REAP_PROCESS = 2,
    CPL_DESCRIPTOR_REMOVE_WORKDIR = 3,
    CPL_DESCRIPTOR_TERMINAL_CHECKS = 4,
};

struct cpl_process_identity {
    int64_t pid;
    uint64_t start_ns;
    uint32_t uid;
    int32_t pgid;
    int32_t sid;
    uint32_t flags;
    uint64_t executable_dev;
    uint64_t executable_ino;
    uint8_t boot_id[CPL_HASH_SIZE];
    uint8_t executable_hash[CPL_HASH_SIZE];
};

struct cpl_batch_descriptor {
    uint32_t kind;
    uint32_t required_steps;
    struct cpl_process_identity target;
};

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
    uint32_t descriptor_count;
    uint64_t normal_limit;
    uint64_t hard_limit;
    uint64_t workdir_parent_dev;
    uint64_t workdir_parent_ino;
    uint64_t workdir_dev;
    uint64_t workdir_ino;
    uint32_t workdir_bound;
    uint32_t no_dependent_artifact;
    uint8_t boot_id[CPL_HASH_SIZE];
    uint8_t executable_hash[CPL_HASH_SIZE];
    uint8_t candidate[CPL_ID_SIZE];
    uint8_t executor[CPL_ID_SIZE];
    uint8_t prior_actor[CPL_ID_SIZE];
    uint8_t authority[CPL_ID_SIZE];
    uint8_t exact_batch[CPL_ID_SIZE];
    uint8_t inherited_batch[CPL_ID_SIZE];
    uint8_t reason[CPL_REASON_SIZE];
    uint8_t workdir_name[CPL_WORKDIR_NAME_SIZE];
    struct cpl_batch_descriptor descriptors[CPL_MAX_BATCH_DESCRIPTORS];
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
    uint32_t descriptor_count;
    uint32_t workdir_bound;
    uint64_t normal_limit;
    uint64_t hard_limit;
    uint64_t workdir_parent_dev;
    uint64_t workdir_parent_ino;
    uint64_t workdir_dev;
    uint64_t workdir_ino;
    uint32_t no_dependent_artifact;
    uint32_t reserved;
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
    uint8_t workdir_name[CPL_WORKDIR_NAME_SIZE];
    struct cpl_batch_descriptor descriptors[CPL_MAX_BATCH_DESCRIPTORS];
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

struct cpl_append_result {
    struct cpl_state state;
    uint8_t hash[CPL_HASH_SIZE];
    uint64_t sequence;
};

struct cpl_create_receipt {
    enum cpl_storage_state state;
    uint8_t intent_hash[CPL_HASH_SIZE];
    uint8_t capability[CPL_HASH_SIZE];
};

struct cpl_workdir_receipt {
    enum cpl_storage_state state;
    uint8_t bound_hash[CPL_HASH_SIZE];
    uint8_t capability[CPL_HASH_SIZE];
    uint64_t workdir_dev;
    uint64_t workdir_ino;
};

struct cpl_delete_authority {
    enum cpl_delete_authority_kind kind;
    uint8_t allocation_nonce[CPL_HASH_SIZE];
    uint8_t certified_hash[CPL_HASH_SIZE];
    uint8_t capability[CPL_HASH_SIZE];
};

struct cpl_delete_receipt {
    enum cpl_storage_state state;
    bool slot_releasable;
    uint8_t reserved[3];
    uint8_t capability[CPL_HASH_SIZE];
};

struct cpl_action_token {
    uint8_t capability[CPL_HASH_SIZE];
    uint8_t batch_nonce[CPL_ID_SIZE];
    uint64_t generation;
};

struct cpl_reap_proof {
    int64_t pid;
    uint8_t certified_hash[CPL_HASH_SIZE];
    uint8_t capability[CPL_HASH_SIZE];
};

int cpl_journal_create_at(int parent_dirfd, const char *journal_name,
    int workdir_parent_dirfd, const char *workdir_name,
    const uint8_t nonce[CPL_HASH_SIZE], uint64_t normal_limit,
    uint64_t hard_limit, cpl_journal **out,
    struct cpl_create_receipt *receipt);
int cpl_journal_open_at(int parent_dirfd, const char *journal_name,
    int workdir_parent_dirfd, const char *workdir_name,
    const uint8_t nonce[CPL_HASH_SIZE], uint64_t normal_limit,
    uint64_t hard_limit, cpl_journal **out);
int cpl_journal_append(cpl_journal *j, const uint8_t *record,
    uint32_t record_len, uint32_t record_class, uint64_t deadline_ns,
    struct cpl_append_result *out);
int cpl_journal_scan(cpl_journal *j, struct cpl_chain *out);
int cpl_journal_certify(cpl_journal *j, uint64_t deadline_ns,
    struct cpl_certified_head *out);
int cpl_lifecycle_apply(const struct cpl_state *current,
    const struct cpl_record *record, struct cpl_state *out);
int cpl_control_frame_encode(uint16_t type,
    const uint8_t allocation_nonce[CPL_HASH_SIZE], const uint8_t *payload,
    uint32_t payload_length, uint8_t *out, uint32_t out_capacity,
    uint32_t *out_length);
int cpl_control_frame_decode(const uint8_t *wire, uint32_t wire_length,
    const uint8_t expected_nonce[CPL_HASH_SIZE],
    struct cpl_control_frame *out);
int cpl_control_frame_write(int fd, uint16_t type,
    const uint8_t allocation_nonce[CPL_HASH_SIZE], const uint8_t *payload,
    uint32_t payload_length, uint64_t deadline_ns);
int cpl_control_frame_read(int fd,
    const uint8_t expected_nonce[CPL_HASH_SIZE], uint64_t deadline_ns,
    struct cpl_control_frame *out);
int cpl_control_phase_accept(uint32_t *inout_phase, uint16_t type,
    bool durable_head_certified);
int cpl_journal_create_workdir(cpl_journal *j,
    int workdir_parent_dirfd, const struct cpl_create_receipt *create_receipt,
    uint64_t deadline_ns, struct cpl_workdir_receipt *receipt);
int cpl_journal_certify_no_dependent_artifact(cpl_journal *j,
    int workdir_parent_dirfd, uint64_t deadline_ns,
    struct cpl_delete_authority *authority);
int cpl_journal_make_delete_authority(cpl_journal *j, uint32_t kind,
    uint64_t deadline_ns, struct cpl_delete_authority *authority);
int cpl_process_observe(int64_t pid, struct cpl_process_identity *out);
int cpl_journal_activate_executor(cpl_journal *j, uint64_t generation,
    const uint8_t executor[CPL_ID_SIZE], int64_t pid,
    uint64_t lease_deadline_ns, uint64_t deadline_ns,
    struct cpl_append_result *out);
int cpl_journal_admit_batch(cpl_journal *j, uint64_t generation,
    const uint8_t executor[CPL_ID_SIZE], const uint8_t batch_nonce[CPL_ID_SIZE],
    const struct cpl_batch_descriptor *descriptors, uint32_t descriptor_count,
    uint64_t deadline_ns, struct cpl_action_token *token,
    struct cpl_append_result *out);
int cpl_journal_execute_batch(cpl_journal *j,
    const struct cpl_action_token *token, int workdir_parent_dirfd,
    uint64_t deadline_ns, uint64_t *completed_steps);
int cpl_journal_complete_batch(cpl_journal *j,
    const struct cpl_action_token *token, uint64_t deadline_ns,
    uint32_t *token_state, struct cpl_append_result *out);
int cpl_journal_abandon_batch(cpl_journal *j,
    const struct cpl_action_token *token);
int cpl_journal_finish_done(cpl_journal *j, uint64_t generation,
    const uint8_t executor[CPL_ID_SIZE], uint64_t deadline_ns,
    struct cpl_append_result *out);
int cpl_journal_retire_executor(cpl_journal *j,
    const uint8_t authority[CPL_ID_SIZE], uint64_t authority_epoch,
    uint64_t authority_deadline_ns, uint64_t deadline_ns,
    struct cpl_append_result *out);
int cpl_journal_replace_retirement_authority(cpl_journal *j,
    const uint8_t authority[CPL_ID_SIZE], uint64_t authority_epoch,
    uint64_t authority_deadline_ns, uint64_t deadline_ns,
    struct cpl_append_result *out);
int cpl_journal_mark_unconfirmed(cpl_journal *j, uint32_t reason_code,
    uint64_t deadline_ns, struct cpl_append_result *out);
int cpl_journal_confirm_executor_reaped(cpl_journal *j,
    uint64_t deadline_ns, struct cpl_reap_proof *proof);
int cpl_journal_reconcile_interrupted_batch(cpl_journal *j,
    const struct cpl_reap_proof *proof, uint64_t deadline_ns,
    struct cpl_append_result *out);
int cpl_journal_prepare_successor(cpl_journal *j,
    const struct cpl_reap_proof *proof, uint64_t generation,
    const uint8_t candidate[CPL_ID_SIZE], uint64_t claim_deadline_ns,
    uint64_t deadline_ns, struct cpl_append_result *out);
int cpl_journal_delete_at(cpl_journal **inout_j, int parent_dirfd,
    const char *journal_name, int workdir_parent_dirfd,
    const char *workdir_name, const struct cpl_delete_authority *authority,
    uint64_t deadline_ns, struct cpl_delete_receipt *receipt);
int cpl_journal_reconcile_absent_after_crash(cpl_journal **inout_j,
    int parent_dirfd, const char *journal_name, int workdir_parent_dirfd,
    const char *workdir_name, const struct cpl_delete_authority *authority,
    uint64_t deadline_ns, struct cpl_delete_receipt *receipt);
void cpl_journal_close(cpl_journal *j);

enum cpl_fault_pause_point {
    CPL_FAULT_BEFORE_WORKDIR_BOUND_APPEND = 1,
    CPL_FAULT_BEFORE_RETIREMENT_EXPIRY_CHECK = 2,
    CPL_FAULT_AFTER_BATCH_ADMISSION_APPEND = 3,
    CPL_FAULT_BEFORE_RETIREMENT_APPEND = 4,
    CPL_FAULT_AFTER_FIRST_DEPENDENT_SCAN = 5,
    CPL_FAULT_BEFORE_ACTIVATION_APPEND = 6,
    CPL_FAULT_BEFORE_SUCCESSOR_APPEND = 7,
    CPL_FAULT_BEFORE_AUTHORITY_REPLACEMENT_APPEND = 8,
    CPL_FAULT_BEFORE_BATCH_COMPLETION_APPEND = 9,
    CPL_FAULT_AFTER_BATCH_COMPLETION_APPEND = 10,
};

enum cpl_fault_create_pause_point {
    CPL_FAULT_BEFORE_CREATE_OPENAT = 1,
    CPL_FAULT_BEFORE_CREATE_PREALLOCATE = 2,
    CPL_FAULT_BEFORE_CREATE_INTENT_WRITE = 3,
    CPL_FAULT_BEFORE_CREATE_FULLFSYNC = 4,
    CPL_FAULT_BEFORE_CREATE_PARENT_FSYNC = 5,
};

enum cpl_unconfirmed_reason {
    CPL_UNCONFIRMED_PROOF_UNAVAILABLE = 1,
    CPL_UNCONFIRMED_NORMAL_REGION_EXHAUSTED = 2,
    CPL_UNCONFIRMED_IDENTITY_UNAVAILABLE = 3,
};
#ifdef CPL_ENABLE_FAULT_INJECTION
int cpl_fault_configure_create_pause(uint32_t point, int notify_fd,
    int wait_fd, uint64_t deadline_ns);
int cpl_fault_configure_lifecycle_pause(cpl_journal *j, uint32_t point,
    int notify_fd, int wait_fd);
int cpl_fault_fail_batch_after_step(cpl_journal *j, uint32_t step);
int cpl_fault_fail_batch_after_effect(cpl_journal *j, uint32_t step);
int cpl_fault_fail_next_workdir_parent_fsync(cpl_journal *j);
int cpl_fault_force_atfork_registration_failure(bool enabled);
int cpl_fault_hold_append_lock(cpl_journal *j, int notify_fd, int wait_fd,
    uint64_t deadline_ns);
int cpl_fault_hold_action_lock(cpl_journal *j, int notify_fd, int wait_fd,
    uint64_t deadline_ns);
int cpl_fault_probe_action_lock(cpl_journal *j, uint64_t deadline_ns);
int cpl_fault_configure_certify_pause(int notify_fd, int wait_fd);
int cpl_fault_append_bytes(cpl_journal *j, const uint8_t *bytes,
    uint32_t length);
int cpl_fault_encode_record(cpl_journal *j, const uint8_t *record,
    uint32_t record_len, uint8_t *out, uint32_t capacity,
    uint32_t *out_len);
int cpl_fault_inject_header_mismatch(cpl_journal *j,
    const uint8_t *record, uint32_t record_len, uint32_t mismatch);
int cpl_fault_try_bound_gate(cpl_journal *j, uint32_t gate_kind,
    const struct cpl_workdir_receipt *receipt, int marker_parent_dirfd,
    const char *marker_name);
int cpl_fault_try_cleanup_gate(const struct cpl_delete_receipt *receipt,
    int marker_parent_dirfd, const char *marker_name);
#endif

#ifdef __cplusplus
}
#endif

#endif
