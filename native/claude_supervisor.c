#include "lifecycle.h"

#include <CommonCrypto/CommonDigest.h>
#include <errno.h>
#include <fcntl.h>
#include <libproc.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/proc.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

extern char **environ;

#define SUPERVISOR_FAIL_DEAD_EXIT 75
#define SUPERVISOR_INJECTED_DEATH_EXIT 86
#define SUPERVISOR_NORMAL_LIMIT (32U * 1024U)
#define SUPERVISOR_HARD_LIMIT (SUPERVISOR_NORMAL_LIMIT + CPL_RECOVERY_BYTES)
#define SUPERVISOR_ENV_CAPACITY 64U
#define SUPERVISOR_NAME_CAPACITY 128U

static const char *const inherited_names[] = {
    "HOME", "USER", "LOGNAME", "TMPDIR", "TMP", "TEMP", "LANG",
    "LC_ALL", "LC_CTYPE", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "CLAUDE_CONFIG_DIR",
};

static const char *const network_proxy_names[] = {
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
};

struct fixed_environment {
    const char *name;
    const char *value;
};

static const struct fixed_environment fixed_environment[] = {
    {"CLAUDE_CODE_SKIP_PROMPT_HISTORY", "1"},
    {"CLAUDE_CODE_ATTRIBUTION_HEADER", "0"},
    {"DISABLE_COMPACT", "1"},
    {"CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1"},
    {"CLAUDE_CODE_DISABLE_CLAUDE_MDS", "1"},
    {"CLAUDE_CODE_DISABLE_BUNDLED_SKILLS", "1"},
    {"CLAUDE_CODE_DISABLE_POLICY_SKILLS", "1"},
    {"CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS", "1"},
    {"ENABLE_CLAUDEAI_MCP_SERVERS", "false"},
    {"CLAUDE_CODE_DISABLE_WORKFLOWS", "1"},
    {"CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL", "1"},
    {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1"},
    {"CLAUDE_CODE_DISABLE_TERMINAL_TITLE", "1"},
};

struct child_environment {
    char *entries[SUPERVISOR_ENV_CAPACITY];
    size_t count;
};

struct bootstrap_values {
    const char *allocation_nonce;
    const char *instance_dir;
    const char *real_cli;
    int control_fd;
    int anchor_fallback_fd;
    bool network_proxy_enabled;
    uint32_t cleanup_injection;
};

enum cleanup_injection {
    CLEANUP_INJECTION_NONE = 0,
    CLEANUP_INJECTION_AFTER_ADMISSION = 1,
    CLEANUP_INJECTION_AFTER_STOP = 2,
    CLEANUP_INJECTION_AFTER_ENUMERATION = 3,
    CLEANUP_INJECTION_AFTER_CONT = 4,
    CLEANUP_INJECTION_AFTER_TERM = 5,
    CLEANUP_INJECTION_AFTER_KILL = 6,
    CLEANUP_INJECTION_ANCHOR_ONLY = 7,
    CLEANUP_INJECTION_ALTERED_EXECUTABLE = 8,
    CLEANUP_INJECTION_REUSED_IDENTITY = 9,
    CLEANUP_INJECTION_UNEXPECTED_DESCENDANT = 10,
    CLEANUP_INJECTION_AFTER_RUNNING = 11,
};

static bool actor_loss_injection(uint32_t injection) {
    return (injection >= CLEANUP_INJECTION_AFTER_ADMISSION &&
        injection <= CLEANUP_INJECTION_AFTER_KILL) ||
        injection == CLEANUP_INJECTION_AFTER_RUNNING;
}

struct cleanup_result {
    bool stop_used;
    bool enumerated_while_stopped;
    bool term_used;
    bool kill_used;
    bool zombie_observed;
    bool absence_enumerated;
    bool anchor_reaped;
    bool task4_completed;
    bool process_batch_preauthorized;
    bool token_retained_through_signals;
    bool process_target_exact;
    bool injection_recovered;
    bool anchor_only_observed;
    bool group_resumed_after_failure;
    uint32_t batch_count;
    uint64_t completed_steps;
    uint64_t done_sequence;
    uint64_t process_batch_admission_sequence;
    uint32_t injection_stage;
    struct cpl_process_identity rejection_observed;
    uint32_t unexpected_group_member_count;
};

struct identity_rejection_evidence {
    uint32_t reason;
    uint32_t unexpected_group_member_count;
    struct cpl_process_identity observed;
};

static bool enumerate_stopped_group(pid_t pgid, pid_t anchor,
    bool *anchor_only);
static bool enumerate_absence_with_unreaped_anchor(pid_t pgid,
    pid_t anchor);
static int wait_unreaped(pid_t pid, bool *observed);
static int retained_group_cleanup(cpl_journal *journal, int directory_fd,
    const struct cpl_process_identity *anchor,
    uint32_t injection, struct cleanup_result *result);
static _Noreturn void inject_actor_death(cpl_journal *journal,
    uint32_t injection);

static uint64_t monotonic_deadline(void) {
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC_RAW, &now) < 0) {
        return 0U;
    }
    return (uint64_t)now.tv_sec * 1000000000ULL +
        (uint64_t)now.tv_nsec + 1000000000ULL;
}

static uint64_t control_deadline(void) {
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC_RAW, &now) < 0) {
        return 0U;
    }
    return (uint64_t)now.tv_sec * 1000000000ULL +
        (uint64_t)now.tv_nsec + 5000000000ULL;
}

static uint64_t lease_deadline(void) {
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC_RAW, &now) < 0) {
        return 0U;
    }
    return (uint64_t)now.tv_sec * 1000000000ULL +
        (uint64_t)now.tv_nsec + 30000000000ULL;
}

static uint64_t selected_lease_deadline(uint32_t injection) {
    struct timespec now;

    if (!actor_loss_injection(injection)) {
        return lease_deadline();
    }
    if (clock_gettime(CLOCK_MONOTONIC_RAW, &now) < 0) {
        return 0U;
    }
    return (uint64_t)now.tv_sec * 1000000000ULL +
        (uint64_t)now.tv_nsec + 250000000ULL;
}

static void bounded_pause(void) {
    const struct timespec duration = {.tv_sec = 0, .tv_nsec = 1000000L};

    (void)nanosleep(&duration, NULL);
}

static int parse_fd(const char *text, int *out) {
    char *end = NULL;
    long value;

    if (text == NULL || out == NULL || *text == '\0') {
        return -1;
    }
    errno = 0;
    value = strtol(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || value < 0 ||
        value > INT_MAX || fcntl((int)value, F_GETFD) < 0) {
        return -1;
    }
    *out = (int)value;
    return 0;
}

static int hex_nibble(char value) {
    if (value >= '0' && value <= '9') {
        return value - '0';
    }
    if (value >= 'a' && value <= 'f') {
        return value - 'a' + 10;
    }
    if (value >= 'A' && value <= 'F') {
        return value - 'A' + 10;
    }
    return -1;
}

static int parse_nonce(const char *text, uint8_t nonce[CPL_HASH_SIZE]) {
    size_t index;

    if (text == NULL || strlen(text) != CPL_HASH_SIZE * 2U) {
        return -1;
    }
    for (index = 0U; index < CPL_HASH_SIZE; ++index) {
        int high = hex_nibble(text[index * 2U]);
        int low = hex_nibble(text[index * 2U + 1U]);

        if (high < 0 || low < 0) {
            return -1;
        }
        nonce[index] = (uint8_t)((unsigned)high << 4U | (unsigned)low);
    }
    return 0;
}

static uint32_t parse_cleanup_injection(void) {
#ifdef CPL_ENABLE_TASK5_INJECTIONS
    static const char *const names[] = {
        "none", "after_admission", "after_stop", "after_enumeration",
        "after_cont", "after_term", "after_kill", "anchor_only",
        "altered_executable_identity", "reused_pid",
        "unexpected_descendant", "after_running",
    };
    const char *selected = getenv("LOCAL_PROXY_TEST_INJECTION");
    uint32_t index;

    if (selected == NULL) {
        return CLEANUP_INJECTION_NONE;
    }
    for (index = 0U; index < sizeof(names) / sizeof(names[0]); ++index) {
        if (strcmp(selected, names[index]) == 0) {
            return index;
        }
    }
    return UINT32_MAX;
#else
    return CLEANUP_INJECTION_NONE;
#endif
}

static bool starts_with(const char *value, const char *prefix) {
    return strncmp(value, prefix, strlen(prefix)) == 0;
}

static bool contains_term(const char *name) {
    static const char *const terms[] = {
        "API_KEY", "OAUTH", "AUTH_TOKEN", "ACCESS_TOKEN", "SECRET_KEY",
        "ACCESS_KEY", "USE_", "USE_BEDROCK", "USE_VERTEX",
        "USE_FOUNDRY", "BEDROCK", "VERTEX", "FOUNDRY", "LITELLM",
        "PROVIDER", "BASE_URL", "ENDPOINT", "API_HOST",
        "CUSTOM_HEADERS", "PROFILE",
    };
    size_t index;

    for (index = 0U; index < sizeof(terms) / sizeof(terms[0]); ++index) {
        if (strstr(name, terms[index]) != NULL) {
            return true;
        }
    }
    return false;
}

static bool authentication_or_provider_override(const char *name) {
    static const char *const cloud_prefixes[] = {
        "AWS_", "GOOGLE_", "GOOGLE_CLOUD_", "AZURE_", "VERTEX",
        "VERTEXAI_", "BEDROCK_", "CLOUDSDK_", "CLOUD_ML_",
    };
    size_t index;

    for (index = 0U;
         index < sizeof(cloud_prefixes) / sizeof(cloud_prefixes[0]); ++index) {
        if (starts_with(name, cloud_prefixes[index])) {
            return true;
        }
    }
    return (starts_with(name, "ANTHROPIC_") ||
        starts_with(name, "CLAUDE_") || starts_with(name, "OPENAI_")) &&
        contains_term(name);
}

static int validate_environment_source(void) {
    char **entry;

    for (entry = environ; *entry != NULL; ++entry) {
        const char *separator = strchr(*entry, '=');
        char name[SUPERVISOR_NAME_CAPACITY];
        size_t length;

        if (separator == NULL) {
            return -1;
        }
        length = (size_t)(separator - *entry);
        if (length == 0U || length >= sizeof(name)) {
            return -1;
        }
        (void)memcpy(name, *entry, length);
        name[length] = '\0';
        if (!starts_with(name, "LOCAL_PROXY_") &&
            authentication_or_provider_override(name)) {
            return -1;
        }
    }
    return 0;
}

static void free_child_environment(struct child_environment *environment) {
    size_t index;

    for (index = 0U; index < environment->count; ++index) {
        free(environment->entries[index]);
        environment->entries[index] = NULL;
    }
    environment->count = 0U;
}

static int add_environment(struct child_environment *environment,
    const char *name, const char *value) {
    size_t name_length;
    size_t value_length;
    char *entry;

    if (environment->count + 1U >= SUPERVISOR_ENV_CAPACITY || name == NULL ||
        value == NULL || strchr(name, '=') != NULL) {
        return -1;
    }
    name_length = strlen(name);
    value_length = strlen(value);
    if (name_length > SIZE_MAX - value_length - 2U) {
        return -1;
    }
    entry = malloc(name_length + value_length + 2U);
    if (entry == NULL) {
        return -1;
    }
    (void)memcpy(entry, name, name_length);
    entry[name_length] = '=';
    (void)memcpy(entry + name_length + 1U, value, value_length + 1U);
    environment->entries[environment->count++] = entry;
    environment->entries[environment->count] = NULL;
    return 0;
}

static int build_child_environment(const char *real_cli,
    bool network_proxy_enabled, struct child_environment *out) {
    char path_value[PATH_MAX + 64U];
    char cli_directory[PATH_MAX];
    const char *separator;
    int rendered;
    size_t directory_length;
    size_t index;

    (void)memset(out, 0, sizeof(*out));
    if (validate_environment_source() < 0 || real_cli == NULL ||
        real_cli[0] != '/') {
        return -1;
    }
    separator = strrchr(real_cli, '/');
    if (separator == NULL || separator == real_cli) {
        return -1;
    }
    directory_length = (size_t)(separator - real_cli);
    if (directory_length >= sizeof(cli_directory)) {
        return -1;
    }
    (void)memcpy(cli_directory, real_cli, directory_length);
    cli_directory[directory_length] = '\0';
    rendered = snprintf(path_value, sizeof(path_value),
        "%s:/usr/bin:/bin:/usr/sbin:/sbin", cli_directory);
    if (rendered < 0 || (size_t)rendered >= sizeof(path_value)) {
        return -1;
    }
    for (index = 0U;
         index < sizeof(inherited_names) / sizeof(inherited_names[0]); ++index) {
        const char *value = getenv(inherited_names[index]);

        if (value != NULL && add_environment(out, inherited_names[index],
                value) < 0) {
            goto fail;
        }
    }
    if (add_environment(out, "PATH", path_value) < 0) {
        goto fail;
    }
    if (network_proxy_enabled) {
        for (index = 0U; index < sizeof(network_proxy_names) /
                sizeof(network_proxy_names[0]); ++index) {
            const char *value = getenv(network_proxy_names[index]);

            if (value != NULL && add_environment(out,
                    network_proxy_names[index], value) < 0) {
                goto fail;
            }
        }
    }
    for (index = 0U; index < sizeof(fixed_environment) /
            sizeof(fixed_environment[0]); ++index) {
        if (add_environment(out, fixed_environment[index].name,
                fixed_environment[index].value) < 0) {
            goto fail;
        }
    }
    return 0;

fail:
    free_child_environment(out);
    return -1;
}

static int parse_bootstrap_values(struct bootstrap_values *out) {
    const char *fd_text;
    const char *network_proxy;
    uint8_t nonce[CPL_HASH_SIZE];

    (void)memset(out, 0, sizeof(*out));
    out->control_fd = -1;
    out->anchor_fallback_fd = -1;
    out->allocation_nonce = getenv("LOCAL_PROXY_ALLOCATION_NONCE");
    out->instance_dir = getenv("LOCAL_PROXY_INSTANCE_DIR");
    out->real_cli = getenv("LOCAL_PROXY_REAL_CLAUDE");
    fd_text = getenv("LOCAL_PROXY_CONTROL_FD");
    network_proxy = getenv("LOCAL_PROXY_NETWORK_PROXY");
    out->cleanup_injection = parse_cleanup_injection();
    if (parse_nonce(out->allocation_nonce, nonce) < 0 ||
        out->instance_dir == NULL || out->instance_dir[0] != '/' ||
        out->real_cli == NULL || out->real_cli[0] != '/' ||
        parse_fd(fd_text, &out->control_fd) < 0 ||
        parse_fd(getenv("LOCAL_PROXY_ANCHOR_CONTROL_FD"),
            &out->anchor_fallback_fd) < 0 || network_proxy == NULL ||
        (strcmp(network_proxy, "0") != 0 &&
         strcmp(network_proxy, "1") != 0)) {
        return -1;
    }
    if (out->cleanup_injection == UINT32_MAX) {
        return -1;
    }
    out->network_proxy_enabled = strcmp(network_proxy, "1") == 0;
    return 0;
}

static int own_supervisor_domain(const struct bootstrap_values *values,
    cpl_journal **out, int *out_directory_fd) {
    struct cpl_append_result activated;
    struct cpl_certified_head certified;
    uint8_t nonce[CPL_HASH_SIZE];
    uint8_t executor[CPL_ID_SIZE] = {0};
    int status;

    *out = NULL;
    *out_directory_fd = -1;
    if (parse_nonce(values->allocation_nonce, nonce) < 0) {
        return -1;
    }
    *out_directory_fd = open(values->instance_dir,
        O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (*out_directory_fd < 0) {
        return -1;
    }
    status = cpl_journal_open_at(*out_directory_fd, "allocation.journal",
        *out_directory_fd, "allocation.workdir", nonce,
        SUPERVISOR_NORMAL_LIMIT, SUPERVISOR_HARD_LIMIT, out);
    if (status == CPL_OK) {
        (void)memcpy(executor, "supervisor", strlen("supervisor"));
        status = cpl_journal_activate_executor(*out, 1U, executor,
            (int64_t)getpid(), selected_lease_deadline(
                values->cleanup_injection), control_deadline(),
            &activated);
    }
    if (status == CPL_OK) {
        status = cpl_journal_certify(*out, monotonic_deadline(), &certified);
    }
    if (status != CPL_OK) {
        if (*out != NULL) {
            cpl_journal_close(*out);
            *out = NULL;
        }
        (void)close(*out_directory_fd);
        *out_directory_fd = -1;
        return -1;
    }
    return 0;
}

static int supervisor_identity_handshake(
    const struct bootstrap_values *values, cpl_journal *journal) {
    struct cpl_process_identity identity;
    struct cpl_control_frame frame;
    struct cpl_bootstrap_head bootstrap;
    struct cpl_certified_head certified;
    uint8_t nonce[CPL_HASH_SIZE];
    uint32_t phase = CPL_CONTROL_PHASE_NONE;
    int status;

    if (parse_nonce(values->allocation_nonce, nonce) < 0) {
        return -1;
    }
    status = cpl_process_observe((int64_t)getpid(), &identity);
    if (status == CPL_OK) {
        status = cpl_journal_bootstrap_append(journal,
            CPL_CONTROL_SUPERVISOR_IDENTITY, (const uint8_t *)&identity,
            (uint32_t)sizeof(identity), control_deadline(), &bootstrap);
    }
    if (status == CPL_OK) {
        status = cpl_control_frame_write(values->control_fd,
            CPL_CONTROL_SUPERVISOR_IDENTITY, nonce,
            (const uint8_t *)&identity, (uint32_t)sizeof(identity),
            control_deadline());
    }
    if (status == CPL_OK) {
        status = cpl_control_phase_accept(&phase,
            CPL_CONTROL_SUPERVISOR_IDENTITY, false);
    }
    if (status == CPL_OK) {
        status = cpl_control_frame_read(values->control_fd, nonce,
            control_deadline(), &frame);
    }
    if (status == CPL_OK) {
        status = cpl_journal_certify(journal, monotonic_deadline(),
            &certified);
    }
    if (status == CPL_OK && frame.type != CPL_CONTROL_IDENTITY_ACK) {
        status = CPL_ERR_CONTROL_PHASE;
    }
    if (status == CPL_OK) {
        status = cpl_control_phase_accept(&phase, frame.type, true);
    }
    return status == CPL_OK ? 0 : -1;
}

static int verified_anchor_path(char out[PATH_MAX]) {
    char executable[PATH_MAX];
    char *separator;
    uint32_t size = (uint32_t)sizeof(executable);
    int result;

    if (_NSGetExecutablePath(executable, &size) != 0) {
        return -1;
    }
    separator = strrchr(executable, '/');
    if (separator == NULL) {
        return -1;
    }
    separator[1] = '\0';
    result = snprintf(out, PATH_MAX, "%sclaude-proxy-anchor", executable);
    return result > 0 && result < PATH_MAX ? 0 : -1;
}

static int verify_real_cli(const char *path,
    struct cpl_cli_armed_identity *expected) {
    struct stat metadata;
    uint8_t digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256_CTX context;
    uint8_t buffer[4096];
    int fd;

    if (path == NULL || path[0] != '/' || expected == NULL) {
        return -1;
    }
    (void)memset(expected, 0, sizeof(*expected));
    fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0 || fstat(fd, &metadata) < 0 || !S_ISREG(metadata.st_mode) ||
        metadata.st_uid != geteuid() || (metadata.st_mode & S_IXUSR) == 0 ||
        CC_SHA256_Init(&context) != 1) {
        if (fd >= 0) {
            (void)close(fd);
        }
        return -1;
    }
    for (;;) {
        ssize_t got = read(fd, buffer, sizeof(buffer));

        if (got < 0 && errno == EINTR) {
            continue;
        }
        if (got < 0 || (got > 0 && CC_SHA256_Update(&context, buffer,
                (CC_LONG)got) != 1)) {
            (void)close(fd);
            return -1;
        }
        if (got == 0) {
            break;
        }
    }
    (void)close(fd);
    if (CC_SHA256_Final(digest, &context) != 1) {
        return -1;
    }
    expected->expected_executable_dev = (uint64_t)metadata.st_dev;
    expected->expected_executable_ino = (uint64_t)metadata.st_ino;
    (void)memcpy(expected->expected_executable_hash, digest, sizeof(digest));
    (void)CC_SHA256(path, (CC_LONG)strlen(path), expected->expected_path_hash);
    return 0;
}

static bool same_incarnation(const struct cpl_process_identity *left,
    const struct cpl_process_identity *right) {
    return left->pid == right->pid && left->start_ns == right->start_ns &&
        left->uid == right->uid && left->pgid == right->pgid &&
        left->sid == right->sid && left->flags == right->flags &&
        memcmp(left->boot_id, right->boot_id, CPL_HASH_SIZE) == 0;
}

static bool same_exact_identity(const struct cpl_process_identity *left,
    const struct cpl_process_identity *right) {
    return same_incarnation(left, right) &&
        left->executable_dev == right->executable_dev &&
        left->executable_ino == right->executable_ino &&
        memcmp(left->executable_hash, right->executable_hash,
            CPL_HASH_SIZE) == 0;
}

static bool matches_expected_cli(const struct cpl_process_identity *actual,
    const struct cpl_cli_armed_identity *armed) {
    return same_incarnation(actual, &armed->member) &&
        actual->executable_dev == armed->expected_executable_dev &&
        actual->executable_ino == armed->expected_executable_ino &&
        memcmp(actual->executable_hash, armed->expected_executable_hash,
            CPL_HASH_SIZE) == 0;
}

static int relay_gate(int external_fd, int internal_fd,
    cpl_journal *journal, const uint8_t nonce[CPL_HASH_SIZE],
    uint16_t identity_type, uint16_t ack_type, uint32_t *phase,
    struct cpl_control_frame *identity) {
    struct cpl_control_frame ack;
    struct cpl_bootstrap_head certified;
    int status = cpl_control_frame_read(internal_fd, nonce,
        control_deadline(), identity);

    if (status == CPL_OK && identity->type != identity_type) {
        status = CPL_ERR_CONTROL_PHASE;
    }
    if (status == CPL_OK) {
        status = cpl_journal_bootstrap_certify(journal, identity_type,
            identity->payload, identity->payload_length, control_deadline(),
            &certified);
    }
    if (status == CPL_OK) {
        status = cpl_control_phase_accept(phase, identity_type, false);
    }
    if (status == CPL_OK) {
        status = cpl_control_frame_write(external_fd, identity_type, nonce,
            identity->payload, identity->payload_length, control_deadline());
    }
    if (status == CPL_OK) {
        status = cpl_control_frame_read(external_fd, nonce,
            control_deadline(), &ack);
    }
    if (status == CPL_OK && ack.type != ack_type) {
        status = CPL_ERR_CONTROL_PHASE;
    }
    if (status == CPL_OK) {
        status = cpl_control_phase_accept(phase, ack.type, true);
    }
    if (status == CPL_OK) {
        status = cpl_control_frame_write(internal_fd, ack_type, nonce, NULL,
            0U, control_deadline());
    }
    return status;
}

static int publish_identity_rejection(int external_fd, cpl_journal *journal,
    const uint8_t nonce[CPL_HASH_SIZE], uint32_t *phase, uint32_t reason,
    uint32_t unexpected_group_member_count,
    const struct cpl_process_identity *observed) {
    struct identity_rejection_evidence evidence;
    struct cpl_bootstrap_head appended;
    struct cpl_bootstrap_head certified;
    int status;

    if (observed == NULL) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memset(&evidence, 0, sizeof(evidence));
    evidence.reason = reason;
    evidence.unexpected_group_member_count = unexpected_group_member_count;
    evidence.observed = *observed;
    status = cpl_journal_bootstrap_append(journal, CPL_CONTROL_ERROR,
        (const uint8_t *)&evidence, (uint32_t)sizeof(evidence),
        control_deadline(), &appended);
    if (status == CPL_OK) {
        status = cpl_journal_bootstrap_certify(journal, CPL_CONTROL_ERROR,
            (const uint8_t *)&evidence, (uint32_t)sizeof(evidence),
            control_deadline(), &certified);
    }
    if (status == CPL_OK && (appended.sequence != certified.sequence ||
            memcmp(appended.hash, certified.hash, CPL_HASH_SIZE) != 0)) {
        status = CPL_ERR_AUTHORITY;
    }
    if (status == CPL_OK) {
        status = cpl_control_phase_accept(phase, CPL_CONTROL_ERROR, false);
    }
    if (status == CPL_OK) {
        status = cpl_control_frame_write(external_fd, CPL_CONTROL_ERROR,
            nonce, (const uint8_t *)&evidence,
            (uint32_t)sizeof(evidence), control_deadline());
    }
    return status;
}

static int launch_anchor(const struct bootstrap_values *values,
    struct child_environment *child_environment, int cli_argc,
    char **cli_argv, cpl_journal *journal, int directory_fd) {
    char anchor_path[PATH_MAX];
    char control_fd[32];
    char fallback_fd[32];
    char **anchor_argv;
    struct cpl_cli_armed_identity expected;
    struct cpl_cli_armed_identity armed;
    struct cpl_control_frame frame;
    struct cpl_control_frame request;
    struct cpl_process_identity anchor_identity;
    struct cpl_process_identity running;
    struct cpl_bootstrap_head bootstrap;
    struct timespec now;
    uint8_t nonce[CPL_HASH_SIZE];
    uint32_t phase = CPL_CONTROL_PHASE_IDENTITY_ACK;
    uint64_t deadline;
    int internal[2] = {-1, -1};
    size_t fixed_count = 12U;
    size_t total;
    size_t index;
    pid_t child;
    int status = CPL_ERR_SYSTEM;
    bool altered_executable_observed = false;

    if (verified_anchor_path(anchor_path) < 0 ||
        verify_real_cli(values->real_cli, &expected) < 0 ||
        parse_nonce(values->allocation_nonce, nonce) < 0 ||
        socketpair(AF_UNIX, SOCK_STREAM, 0, internal) < 0 ||
        snprintf(control_fd, sizeof(control_fd), "%d", internal[1]) < 0 ||
        snprintf(fallback_fd, sizeof(fallback_fd), "%d",
            values->anchor_fallback_fd) < 0) {
        return -1;
    }
    total = fixed_count + (size_t)cli_argc + 1U;
    anchor_argv = calloc(total, sizeof(*anchor_argv));
    if (anchor_argv == NULL) {
        return -1;
    }
    anchor_argv[0] = anchor_path;
    anchor_argv[1] = "--allocation-nonce";
    anchor_argv[2] = (char *)values->allocation_nonce;
    anchor_argv[3] = "--instance-dir";
    anchor_argv[4] = (char *)values->instance_dir;
    anchor_argv[5] = "--control-fd";
    anchor_argv[6] = control_fd;
    anchor_argv[7] = "--real-cli";
    anchor_argv[8] = (char *)values->real_cli;
    anchor_argv[9] = "--fallback-control-fd";
    anchor_argv[10] = fallback_fd;
    anchor_argv[11] = "--";
    for (index = 0U; index < (size_t)cli_argc; ++index) {
        anchor_argv[fixed_count + index] = cli_argv[index];
    }
    child = fork();
    if (child < 0) {
        free(anchor_argv);
        (void)close(internal[0]);
        (void)close(internal[1]);
        return -1;
    }
    if (child == 0) {
        (void)close(values->control_fd);
        (void)close(internal[0]);
        if (fcntl(values->anchor_fallback_fd, F_SETFD, 0) < 0) {
            _exit(SUPERVISOR_FAIL_DEAD_EXIT);
        }
        execve(anchor_path, anchor_argv, child_environment->entries);
        _exit(SUPERVISOR_FAIL_DEAD_EXIT);
    }
    (void)close(internal[1]);
    (void)close(values->anchor_fallback_fd);
    free(anchor_argv);
    status = relay_gate(values->control_fd, internal[0], journal, nonce,
        CPL_CONTROL_ANCHOR_IDENTITY, CPL_CONTROL_ANCHOR_ACK, &phase, &frame);
    if (status == CPL_OK && frame.payload_length == sizeof(anchor_identity)) {
        (void)memcpy(&anchor_identity, frame.payload, sizeof(anchor_identity));
        if (anchor_identity.pid != child || anchor_identity.pid !=
                anchor_identity.pgid || anchor_identity.pid !=
                anchor_identity.sid) {
            status = CPL_ERR_PROCESS_IDENTITY;
        }
    } else if (status == CPL_OK) {
        status = CPL_ERR_CONTROL_PAYLOAD;
    }
    if (status == CPL_OK) {
        status = relay_gate(values->control_fd, internal[0], journal, nonce,
            CPL_CONTROL_CLI_ARMED, CPL_CONTROL_ARMED_ACK, &phase, &frame);
    }
    if (status == CPL_OK && frame.payload_length == sizeof(armed)) {
        (void)memcpy(&armed, frame.payload, sizeof(armed));
        if (armed.member.pgid != anchor_identity.pgid ||
            armed.member.sid != anchor_identity.sid ||
            armed.expected_executable_dev !=
                expected.expected_executable_dev ||
            armed.expected_executable_ino !=
                expected.expected_executable_ino ||
            memcmp(armed.expected_executable_hash,
                expected.expected_executable_hash, CPL_HASH_SIZE) != 0 ||
            memcmp(armed.expected_path_hash, expected.expected_path_hash,
                CPL_HASH_SIZE) != 0) {
            status = CPL_ERR_PROCESS_IDENTITY;
        }
    } else if (status == CPL_OK) {
        status = CPL_ERR_CONTROL_PAYLOAD;
    }
    if (clock_gettime(CLOCK_MONOTONIC_RAW, &now) < 0) {
        status = CPL_ERR_SYSTEM;
        deadline = 0U;
    } else {
        deadline = (uint64_t)now.tv_sec * 1000000000ULL +
            (uint64_t)now.tv_nsec + 5000000000ULL;
    }
    while (status == CPL_OK) {
        status = cpl_process_observe(armed.member.pid, &running);
        if (status == CPL_OK) {
            if (values->cleanup_injection ==
                    CLEANUP_INJECTION_ALTERED_EXECUTABLE) {
                if ((running.executable_dev !=
                        armed.member.executable_dev ||
                     running.executable_ino !=
                        armed.member.executable_ino ||
                     memcmp(running.executable_hash,
                        armed.member.executable_hash,
                        CPL_HASH_SIZE) != 0) &&
                    !matches_expected_cli(&running, &armed)) {
                    altered_executable_observed = true;
                    break;
                }
            } else if (matches_expected_cli(&running, &armed)) {
                break;
            }
        }
        if (clock_gettime(CLOCK_MONOTONIC_RAW, &now) < 0 ||
            (uint64_t)now.tv_sec * 1000000000ULL +
                (uint64_t)now.tv_nsec >= deadline) {
            status = CPL_ERR_CERTIFY_TIMEOUT;
            break;
        }
        bounded_pause();
        status = CPL_OK;
    }
    if (status == CPL_OK && altered_executable_observed) {
        status = publish_identity_rejection(values->control_fd, journal,
            nonce, &phase, CLEANUP_INJECTION_ALTERED_EXECUTABLE, 0U,
            &running);
        (void)close(internal[0]);
        return status == CPL_OK ? -1 : status;
    }
    if (status == CPL_OK) {
        status = cpl_journal_bootstrap_append(journal,
            CPL_CONTROL_CLI_RUNNING, (const uint8_t *)&running,
            (uint32_t)sizeof(running), control_deadline(), &bootstrap);
    }
    if (status == CPL_OK) {
        status = cpl_control_phase_accept(&phase, CPL_CONTROL_CLI_RUNNING,
            false);
    }
    if (status == CPL_OK) {
        status = cpl_control_frame_write(internal[0],
            CPL_CONTROL_CLI_RUNNING, nonce, (const uint8_t *)&running,
            (uint32_t)sizeof(running), control_deadline());
    }
    if (status == CPL_OK) {
        status = cpl_control_frame_write(values->control_fd,
            CPL_CONTROL_CLI_RUNNING, nonce, (const uint8_t *)&running,
            (uint32_t)sizeof(running), control_deadline());
    }
    if (status == CPL_OK) {
        status = cpl_control_frame_read(values->control_fd, nonce,
            lease_deadline(), &request);
    }
    if (status == CPL_OK && values->cleanup_injection ==
            CLEANUP_INJECTION_AFTER_RUNNING) {
        inject_actor_death(journal, values->cleanup_injection);
    }
    if (status == CPL_OK && request.type != CPL_CONTROL_CLEANUP_REQUEST) {
        status = CPL_ERR_CONTROL_PHASE;
    }
    if (status == CPL_OK && request.payload_length !=
            (uint32_t)sizeof(struct cpl_cleanup_ack)) {
        status = CPL_ERR_CONTROL_PAYLOAD;
    }
    if (status == CPL_OK) {
        status = cpl_control_phase_accept(&phase, request.type, false);
    }
    if (status == CPL_OK) {
        status = cpl_journal_bootstrap_certify(journal,
            CPL_CONTROL_CLEANUP_REQUEST, NULL, 0U,
            control_deadline(), &bootstrap);
    }
    if (status == CPL_OK) {
        struct cpl_cleanup_ack expected;

        (void)memcpy(&expected, request.payload, sizeof(expected));
        if (expected.sequence != bootstrap.sequence ||
            memcmp(expected.hash, bootstrap.hash, CPL_HASH_SIZE) != 0) {
            status = CPL_ERR_AUTHORITY;
        }
    }
    if (status == CPL_OK) {
        status = cpl_control_phase_accept(&phase,
            CPL_CONTROL_CLEANUP_ACK, true);
    }
    if (status == CPL_OK) {
        struct cpl_cleanup_ack cleanup_ack;

        (void)memset(&cleanup_ack, 0, sizeof(cleanup_ack));
        cleanup_ack.sequence = bootstrap.sequence;
        (void)memcpy(cleanup_ack.hash, bootstrap.hash, CPL_HASH_SIZE);
        status = cpl_control_frame_write(values->control_fd,
            CPL_CONTROL_CLEANUP_ACK, nonce,
            (const uint8_t *)&cleanup_ack,
            (uint32_t)sizeof(cleanup_ack),
            control_deadline());
    }
    if (status == CPL_OK) {
        status = cpl_control_frame_write(internal[0],
            CPL_CONTROL_CLEANUP_REQUEST, nonce, NULL, 0U,
            control_deadline());
    }
    if (status == CPL_OK) {
        struct cleanup_result cleanup;
        struct cpl_cleanup_evidence evidence;

        (void)memset(&cleanup, 0, sizeof(cleanup));
        status = retained_group_cleanup(journal, directory_fd,
            &anchor_identity, values->cleanup_injection, &cleanup);
        if (status != CPL_OK &&
            values->cleanup_injection >=
                CLEANUP_INJECTION_ALTERED_EXECUTABLE &&
            values->cleanup_injection <=
                CLEANUP_INJECTION_UNEXPECTED_DESCENDANT) {
            (void)publish_identity_rejection(values->control_fd, journal,
                nonce, &phase, values->cleanup_injection,
                cleanup.unexpected_group_member_count,
                &cleanup.rejection_observed);
            (void)close(internal[0]);
            return -1;
        }
        (void)memset(&evidence, 0, sizeof(evidence));
        if (cleanup.stop_used) {
            evidence.flags |= CPL_CLEANUP_STOP_USED;
        }
        if (cleanup.enumerated_while_stopped) {
            evidence.flags |= CPL_CLEANUP_STOPPED_ENUMERATED;
        }
        if (cleanup.term_used) {
            evidence.flags |= CPL_CLEANUP_TERM_USED;
        }
        if (cleanup.kill_used) {
            evidence.flags |= CPL_CLEANUP_KILL_USED;
        }
        if (cleanup.zombie_observed) {
            evidence.flags |= CPL_CLEANUP_ZOMBIE_OBSERVED;
        }
        if (cleanup.absence_enumerated) {
            evidence.flags |= CPL_CLEANUP_ABSENCE_ENUMERATED |
                CPL_CLEANUP_GROUP_ENUMERATION_COMPLETE;
        }
        if (cleanup.anchor_reaped) {
            evidence.flags |= CPL_CLEANUP_ANCHOR_REAPED;
        }
        if (cleanup.task4_completed) {
            evidence.flags |= CPL_CLEANUP_TASK4_DONE;
        }
        if (cleanup.process_batch_preauthorized) {
            evidence.flags |= CPL_CLEANUP_PROCESS_BATCH_PREAUTHORIZED;
        }
        if (cleanup.token_retained_through_signals) {
            evidence.flags |= CPL_CLEANUP_TOKEN_RETAINED_THROUGH_SIGNALS;
        }
        if (cleanup.process_target_exact) {
            evidence.flags |= CPL_CLEANUP_PROCESS_TARGET_EXACT;
        }
        if (cleanup.injection_recovered) {
            evidence.flags |= CPL_CLEANUP_INJECTION_RECOVERED;
        }
        if (cleanup.anchor_only_observed) {
            evidence.flags |= CPL_CLEANUP_ANCHOR_ONLY_OBSERVED;
        }
        if (cleanup.group_resumed_after_failure) {
            evidence.flags |= CPL_CLEANUP_GROUP_RESUMED_AFTER_FAILURE;
        }
        evidence.batch_count = cleanup.batch_count;
        evidence.completed_steps = cleanup.completed_steps;
        evidence.done_sequence = cleanup.done_sequence;
        evidence.process_batch_admission_sequence =
            cleanup.process_batch_admission_sequence;
        evidence.injection_stage = cleanup.injection_stage;
        if (status == CPL_OK) {
            status = cpl_journal_bootstrap_append(journal,
                CPL_CONTROL_CLEANUP_RESULT, (const uint8_t *)&evidence,
                (uint32_t)sizeof(evidence), control_deadline(), &bootstrap);
        }
        if (status == CPL_OK) {
            status = cpl_control_phase_accept(&phase,
                CPL_CONTROL_CLEANUP_RESULT, false);
        }
        if (status == CPL_OK) {
            status = cpl_control_frame_write(values->control_fd,
                CPL_CONTROL_CLEANUP_RESULT, nonce,
                (const uint8_t *)&evidence, (uint32_t)sizeof(evidence),
                control_deadline());
        }
    }
    (void)close(internal[0]);
    return status == CPL_OK ? 0 : -1;
}

static int wait_unreaped(pid_t pid, bool *observed) {
    uint64_t deadline = monotonic_deadline();

    *observed = false;
    while (deadline > 0U) {
        siginfo_t information;

        (void)memset(&information, 0, sizeof(information));
        if (waitid(P_PID, (id_t)pid, &information,
                WEXITED | WNOHANG | WNOWAIT) < 0) {
            return -1;
        }
        if (information.si_pid == pid) {
            *observed = true;
            return 0;
        }
        if (monotonic_deadline() == 0U ||
            monotonic_deadline() - 1000000000ULL >= deadline) {
            return -1;
        }
        bounded_pause();
    }
    return -1;
}

struct bounded_group_members {
    pid_t *items;
    size_t count;
};

static bool list_group_members(pid_t pgid,
    struct bounded_group_members *out) {
    enum { MAX_GROUP_MEMBERS = 4096 };
    size_t capacity = 16U;

    (void)memset(out, 0, sizeof(*out));
    while (capacity <= MAX_GROUP_MEMBERS) {
        pid_t *first = calloc(capacity, sizeof(*first));
        pid_t *second = calloc(capacity, sizeof(*second));
        int listed;
        int confirmed;

        if (first == NULL || second == NULL) {
            free(first);
            free(second);
            return false;
        }
        listed = proc_listpgrppids(pgid, first,
            (int)(capacity * sizeof(*first)));
        confirmed = proc_listpgrppids(pgid, second,
            (int)(capacity * sizeof(*second)));
        if (listed < 0 || confirmed < 0 || (size_t)listed >= capacity ||
            (size_t)confirmed >= capacity || confirmed > listed) {
            free(first);
            free(second);
            if (listed < 0 || confirmed < 0 || capacity == MAX_GROUP_MEMBERS) {
                return false;
            }
            capacity *= 2U;
            continue;
        }
        free(first);
        out->items = second;
        out->count = (size_t)confirmed;
        return true;
    }
    return false;
}

static void free_group_members(struct bounded_group_members *members) {
    free(members->items);
    members->items = NULL;
    members->count = 0U;
}

static bool enumerate_stopped_group(pid_t pgid, pid_t anchor,
    bool *anchor_only) {
    uint64_t deadline = monotonic_deadline();

    if (anchor_only == NULL) {
        return false;
    }
    *anchor_only = false;
    for (;;) {
        struct bounded_group_members members;
        size_t index;
        unsigned present = 0U;
        bool anchor_found = false;
        bool all_stopped = true;

        if (list_group_members(pgid, &members)) {
            for (index = 0U; index < members.count; ++index) {
                struct proc_bsdinfo information;

                if (members.items[index] <= 0) {
                    continue;
                }
                ++present;
                anchor_found = anchor_found || members.items[index] == anchor;
                if (proc_pidinfo(members.items[index], PROC_PIDTBSDINFO, 0U,
                        &information, (int)sizeof(information)) !=
                        (int)sizeof(information) ||
                    information.pbi_status != SSTOP) {
                    all_stopped = false;
                }
            }
            if (present >= 1U && anchor_found && all_stopped) {
                *anchor_only = present == 1U;
                free_group_members(&members);
                return true;
            }
            free_group_members(&members);
        }
        if (monotonic_deadline() == 0U ||
            monotonic_deadline() - 1000000000ULL >= deadline) {
            return false;
        }
        bounded_pause();
    }
}

static bool enumerate_absence_with_unreaped_anchor(pid_t pgid,
    pid_t anchor) {
    uint64_t deadline = monotonic_deadline();

    for (;;) {
        struct bounded_group_members members;
        size_t index;
        bool anchor_seen = false;
        bool unexpected = false;

        if (list_group_members(pgid, &members)) {
            for (index = 0U; index < members.count; ++index) {
                if (members.items[index] <= 0) {
                    continue;
                }
                if (members.items[index] != anchor) {
                    unexpected = true;
                    continue;
                }
                anchor_seen = true;
            }
            if (!unexpected && (members.count == 0U || anchor_seen)) {
                free_group_members(&members);
                return true;
            }
            free_group_members(&members);
        }
        if (monotonic_deadline() == 0U ||
            monotonic_deadline() - 1000000000ULL >= deadline) {
            return false;
        }
        bounded_pause();
    }
}

static int complete_cleanup_batch(cpl_journal *journal, int directory_fd,
    const char *batch_name, const struct cpl_batch_descriptor *descriptors,
    uint32_t descriptor_count, struct cleanup_result *result) {
    struct cpl_action_token token;
    struct cpl_append_result admitted;
    struct cpl_append_result completed;
    uint8_t executor[CPL_ID_SIZE] = {0};
    uint8_t batch_nonce[CPL_ID_SIZE] = {0};
    uint32_t token_state = CPL_ACTION_TOKEN_RETAINED;
    uint64_t completed_steps = 0U;
    size_t batch_length = strlen(batch_name);
    int status;

    if (batch_length == 0U || batch_length >= CPL_ID_SIZE) {
        return CPL_ERR_INVALID_ARGUMENT;
    }
    (void)memcpy(executor, "supervisor", strlen("supervisor"));
    (void)memcpy(batch_nonce, batch_name, batch_length);
    status = cpl_journal_admit_batch(journal, 1U, executor, batch_nonce,
        descriptors, descriptor_count, control_deadline(), &token,
        &admitted);
    if (status != CPL_OK) {
        return status;
    }
    status = cpl_journal_execute_batch(journal, &token, directory_fd,
        control_deadline(), &completed_steps);
    if (status != CPL_OK) {
        return status;
    }
    status = cpl_journal_complete_batch(journal, &token,
        control_deadline(), &token_state, &completed);
    if (status != CPL_OK || token_state != CPL_ACTION_TOKEN_CONSUMED) {
        return status == CPL_OK ? CPL_ERR_BATCH_TOKEN : status;
    }
    ++result->batch_count;
    result->completed_steps = completed_steps;
    return CPL_OK;
}

static bool observe_retained_anchor(
    const struct cpl_process_identity *anchor,
    struct cpl_process_identity *observed) {
    struct proc_bsdinfo process;

    return anchor != NULL && observed != NULL && anchor->pid > 0 &&
        anchor->pid == anchor->pgid && anchor->pid == anchor->sid &&
        cpl_process_observe(anchor->pid, observed) == CPL_OK &&
        proc_pidinfo((int)anchor->pid, PROC_PIDTBSDINFO, 0U, &process,
            (int)sizeof(process)) == (int)sizeof(process) &&
        process.pbi_ppid == (uint32_t)getpid();
}

static bool retained_anchor_is_exact(
    const struct cpl_process_identity *anchor) {
    struct cpl_process_identity observed;

    return observe_retained_anchor(anchor, &observed) &&
        same_exact_identity(anchor, &observed);
}

static void retain_failed_cleanup_actor(
    const struct cpl_process_identity *anchor, bool group_stopped) {
    for (;;) {
        if (group_stopped && retained_anchor_is_exact(anchor) &&
            killpg(anchor->pgid, SIGCONT) == 0) {
            group_stopped = false;
        }
        bounded_pause();
    }
}

static bool wait_for_anchor_only_group(
    const struct cpl_process_identity *anchor) {
    uint64_t deadline = control_deadline();

    while (deadline > 0U) {
        struct bounded_group_members members;
        size_t index;
        unsigned present = 0U;
        bool exact_anchor = false;

        if (retained_anchor_is_exact(anchor) &&
            list_group_members(anchor->pgid, &members)) {
            for (index = 0U; index < members.count; ++index) {
                if (members.items[index] <= 0) {
                    continue;
                }
                ++present;
                exact_anchor = exact_anchor ||
                    members.items[index] == anchor->pid;
            }
            free_group_members(&members);
            if (present == 1U && exact_anchor) {
                return true;
            }
        }
        if (monotonic_deadline() == 0U ||
            monotonic_deadline() - 1000000000ULL >= deadline) {
            return false;
        }
        bounded_pause();
    }
    return false;
}

static uint32_t observe_unexpected_group_members(
    const struct cpl_process_identity *anchor) {
    uint64_t deadline = control_deadline();

    while (deadline > 0U) {
        struct bounded_group_members members;
        size_t index;
        unsigned present = 0U;
        bool exact_anchor = false;

        if (retained_anchor_is_exact(anchor) &&
            list_group_members(anchor->pgid, &members)) {
            for (index = 0U; index < members.count; ++index) {
                if (members.items[index] <= 0) {
                    continue;
                }
                ++present;
                exact_anchor = exact_anchor ||
                    members.items[index] == anchor->pid;
            }
            free_group_members(&members);
            if (exact_anchor && present > 2U) {
                return present - 2U;
            }
        }
        if (monotonic_deadline() == 0U ||
            monotonic_deadline() - 1000000000ULL >= deadline) {
            return 0U;
        }
        bounded_pause();
    }
    return 0U;
}

static int complete_admitted_process_batch(cpl_journal *journal,
    int directory_fd, const struct cpl_action_token *token,
    struct cleanup_result *result) {
    struct cpl_append_result completed;
    uint32_t token_state = CPL_ACTION_TOKEN_RETAINED;
    uint64_t completed_steps = 0U;
    int status = cpl_journal_execute_batch(journal, token, directory_fd,
        control_deadline(), &completed_steps);

    if (status != CPL_OK) {
        return status;
    }
    status = cpl_journal_complete_batch(journal, token,
        control_deadline(), &token_state, &completed);
    if (status != CPL_OK || token_state != CPL_ACTION_TOKEN_CONSUMED) {
        return status == CPL_OK ? CPL_ERR_BATCH_TOKEN : status;
    }
    ++result->batch_count;
    result->completed_steps = completed_steps;
    return CPL_OK;
}

static _Noreturn void inject_actor_death(cpl_journal *journal,
    uint32_t injection) {
    struct cpl_bootstrap_head appended;
    struct cpl_bootstrap_head certified;
    uint32_t reason = 100U + injection;

    if (journal == NULL || !actor_loss_injection(injection) ||
        cpl_journal_bootstrap_append(journal, CPL_CONTROL_ERROR,
            (const uint8_t *)&reason, (uint32_t)sizeof(reason),
            control_deadline(), &appended) != CPL_OK ||
        cpl_journal_bootstrap_certify(journal, CPL_CONTROL_ERROR,
            (const uint8_t *)&reason, (uint32_t)sizeof(reason),
            control_deadline(), &certified) != CPL_OK ||
        appended.sequence != certified.sequence ||
        memcmp(appended.hash, certified.hash, CPL_HASH_SIZE) != 0) {
        _exit(SUPERVISOR_FAIL_DEAD_EXIT);
    }
    _exit(SUPERVISOR_INJECTED_DEATH_EXIT);
}

static int retained_group_cleanup(cpl_journal *journal, int directory_fd,
    const struct cpl_process_identity *anchor,
    uint32_t injection, struct cleanup_result *result) {
    struct cpl_action_token process_token;
    struct cpl_append_result admitted;
    struct cpl_batch_descriptor descriptor;
    struct cpl_append_result done;
    uint8_t executor[CPL_ID_SIZE] = {0};
    uint8_t batch_nonce[CPL_ID_SIZE] = {0};
    bool group_stopped = false;
    bool anchor_only = false;
    int status;

    if (journal == NULL || result == NULL) {
        return CPL_ERR_PROCESS_IDENTITY;
    }
    if (injection == CLEANUP_INJECTION_REUSED_IDENTITY) {
        if (!observe_retained_anchor(anchor, &result->rejection_observed)) {
            return CPL_ERR_PROCESS_IDENTITY;
        }
        ++result->rejection_observed.start_ns;
        if (!same_exact_identity(anchor, &result->rejection_observed)) {
            return CPL_ERR_PROCESS_IDENTITY;
        }
    } else if (injection == CLEANUP_INJECTION_UNEXPECTED_DESCENDANT) {
        result->unexpected_group_member_count =
            observe_unexpected_group_members(anchor);
        if (result->unexpected_group_member_count > 0U) {
            (void)observe_retained_anchor(anchor,
                &result->rejection_observed);
            return CPL_ERR_PROCESS_IDENTITY;
        }
    }
    if (!retained_anchor_is_exact(anchor)) {
        return CPL_ERR_PROCESS_IDENTITY;
    }
    (void)memset(&descriptor, 0, sizeof(descriptor));
    descriptor.kind = CPL_DESCRIPTOR_PROCESS_ABSENT;
    descriptor.target = *anchor;
    (void)memcpy(executor, "supervisor", strlen("supervisor"));
    (void)memcpy(batch_nonce, "process-absent", strlen("process-absent"));
    status = cpl_journal_admit_batch(journal, 1U, executor, batch_nonce,
        &descriptor, 1U, control_deadline(), &process_token, &admitted);
    if (status != CPL_OK) {
        return status;
    }
    result->process_batch_preauthorized = true;
    result->process_target_exact = same_exact_identity(
        &descriptor.target, anchor);
    result->process_batch_admission_sequence = admitted.sequence;
    result->injection_stage = injection;
    if (injection == CLEANUP_INJECTION_AFTER_ADMISSION) {
        inject_actor_death(journal, injection);
    }
    if (injection == CLEANUP_INJECTION_ANCHOR_ONLY &&
        !wait_for_anchor_only_group(anchor)) {
        retain_failed_cleanup_actor(anchor, false);
    }
    if (killpg(anchor->pgid, SIGSTOP) < 0) {
        retain_failed_cleanup_actor(anchor, false);
    }
    group_stopped = true;
    result->stop_used = true;
    if (injection == CLEANUP_INJECTION_AFTER_STOP) {
        inject_actor_death(journal, injection);
    }
    result->enumerated_while_stopped = enumerate_stopped_group(
        anchor->pgid, (pid_t)anchor->pid, &anchor_only);
    result->anchor_only_observed = anchor_only;
    if (!result->enumerated_while_stopped) {
        retain_failed_cleanup_actor(anchor, group_stopped);
    }
    if (injection == CLEANUP_INJECTION_AFTER_ENUMERATION) {
        inject_actor_death(journal, injection);
    }
    if (killpg(anchor->pgid, SIGCONT) < 0) {
        retain_failed_cleanup_actor(anchor, group_stopped);
    }
    if (injection == CLEANUP_INJECTION_AFTER_CONT) {
        inject_actor_death(journal, injection);
    }
    if (killpg(anchor->pgid, SIGTERM) < 0) {
        retain_failed_cleanup_actor(anchor, false);
    }
    result->term_used = true;
    if (injection == CLEANUP_INJECTION_AFTER_TERM) {
        inject_actor_death(journal, injection);
    }
    if (wait_unreaped((pid_t)anchor->pid, &result->zombie_observed) < 0) {
        if (killpg(anchor->pgid, SIGKILL) < 0) {
            retain_failed_cleanup_actor(anchor, false);
        }
        result->kill_used = true;
        if (injection == CLEANUP_INJECTION_AFTER_KILL) {
            inject_actor_death(journal, injection);
        }
        if (wait_unreaped((pid_t)anchor->pid,
                &result->zombie_observed) < 0) {
            retain_failed_cleanup_actor(anchor, false);
        }
    }
    if (!result->zombie_observed) {
        retain_failed_cleanup_actor(anchor, false);
    }
    result->absence_enumerated = enumerate_absence_with_unreaped_anchor(
        anchor->pgid, (pid_t)anchor->pid);
    if (!result->absence_enumerated) {
        retain_failed_cleanup_actor(anchor, false);
    }
    result->token_retained_through_signals = true;
    status = complete_admitted_process_batch(journal, directory_fd,
        &process_token, result);
    if (status != CPL_OK) {
        retain_failed_cleanup_actor(anchor, false);
    }
    (void)memset(&descriptor, 0, sizeof(descriptor));
    descriptor.kind = CPL_DESCRIPTOR_REAP_PROCESS;
    descriptor.required_steps = CPL_STEP_PROCESS_ABSENT;
    descriptor.target = *anchor;
    status = complete_cleanup_batch(journal, directory_fd, "anchor-reap",
        &descriptor, 1U, result);
    if (status != CPL_OK) {
        return status;
    }
    result->anchor_reaped = true;
    (void)memset(&descriptor, 0, sizeof(descriptor));
    descriptor.kind = CPL_DESCRIPTOR_REMOVE_WORKDIR;
    descriptor.required_steps = CPL_STEP_PROCESS_ABSENT |
        CPL_STEP_EXECUTOR_REAPED;
    status = complete_cleanup_batch(journal, directory_fd,
        "workdir-remove", &descriptor, 1U, result);
    if (status != CPL_OK) {
        return status;
    }
    (void)memset(&descriptor, 0, sizeof(descriptor));
    descriptor.kind = CPL_DESCRIPTOR_TERMINAL_CHECKS;
    descriptor.required_steps = CPL_STEP_PROCESS_ABSENT |
        CPL_STEP_EXECUTOR_REAPED | CPL_STEP_WORKDIR_REMOVED;
    status = complete_cleanup_batch(journal, directory_fd,
        "terminal-checks", &descriptor, 1U, result);
    if (status != CPL_OK || result->completed_steps !=
            CPL_ALL_COMPLETED_STEPS) {
        return status == CPL_OK ? CPL_ERR_PRECONDITION : status;
    }
    (void)memcpy(executor, "supervisor", strlen("supervisor"));
    status = cpl_journal_finish_done(journal, 1U, executor,
        control_deadline(), &done);
    if (status != CPL_OK) {
        return status;
    }
    result->done_sequence = done.sequence;
    result->task4_completed = true;
    return CPL_OK;
}


int main(int argc, char **argv) {
    struct bootstrap_values bootstrap;
    struct child_environment child_environment;
    cpl_journal *journal = NULL;
    int directory_fd = -1;
    int cli_argc;
    char **cli_argv;
    int status;
    struct sigaction ignored;

    (void)memset(&ignored, 0, sizeof(ignored));
    ignored.sa_handler = SIG_IGN;
    (void)sigemptyset(&ignored.sa_mask);
    if (sigaction(SIGPIPE, &ignored, NULL) < 0) {
        return SUPERVISOR_FAIL_DEAD_EXIT;
    }

    if (parse_bootstrap_values(&bootstrap) < 0) {
        return SUPERVISOR_FAIL_DEAD_EXIT;
    }
    cli_argc = argc - 1;
    cli_argv = argv + 1;
    if (cli_argc <= 0 || own_supervisor_domain(&bootstrap, &journal,
            &directory_fd) < 0 || build_child_environment(bootstrap.real_cli,
            bootstrap.network_proxy_enabled, &child_environment) < 0) {
        if (journal != NULL) {
            cpl_journal_close(journal);
        }
        if (directory_fd >= 0) {
            (void)close(directory_fd);
        }
        return SUPERVISOR_FAIL_DEAD_EXIT;
    }
    if (supervisor_identity_handshake(&bootstrap, journal) < 0) {
        free_child_environment(&child_environment);
        cpl_journal_close(journal);
        (void)close(directory_fd);
        return SUPERVISOR_FAIL_DEAD_EXIT;
    }
    status = launch_anchor(&bootstrap, &child_environment, cli_argc, cli_argv,
        journal, directory_fd);
    free_child_environment(&child_environment);
    cpl_journal_close(journal);
    (void)close(directory_fd);
    return status < 0 ? SUPERVISOR_FAIL_DEAD_EXIT : status;
}
