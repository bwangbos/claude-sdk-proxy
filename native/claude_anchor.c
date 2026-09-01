#include "lifecycle.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

extern char **environ;

#define ANCHOR_FAIL_DEAD_EXIT 75
#define ANCHOR_NORMAL_LIMIT (32U * 1024U)
#define ANCHOR_HARD_LIMIT (ANCHOR_NORMAL_LIMIT + CPL_RECOVERY_BYTES)

struct anchor_arguments {
    const char *allocation_nonce;
    const char *instance_dir;
    const char *real_cli;
    int control_fd;
    int cli_index;
};

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

static int parse_arguments(int argc, char **argv,
    struct anchor_arguments *out) {
    int index;

    if (out == NULL) {
        return -1;
    }
    (void)memset(out, 0, sizeof(*out));
    out->control_fd = -1;
    for (index = 1; index < argc; ++index) {
        if (strcmp(argv[index], "--") == 0) {
            out->cli_index = index + 1;
            break;
        }
        if (index + 1 >= argc) {
            return -1;
        }
        if (strcmp(argv[index], "--allocation-nonce") == 0) {
            out->allocation_nonce = argv[++index];
        } else if (strcmp(argv[index], "--instance-dir") == 0) {
            out->instance_dir = argv[++index];
        } else if (strcmp(argv[index], "--control-fd") == 0) {
            if (parse_fd(argv[++index], &out->control_fd) < 0) {
                return -1;
            }
        } else if (strcmp(argv[index], "--real-cli") == 0) {
            out->real_cli = argv[++index];
        } else {
            return -1;
        }
    }
    return out->allocation_nonce != NULL && out->instance_dir != NULL &&
        out->instance_dir[0] == '/' && out->real_cli != NULL &&
        out->real_cli[0] == '/' && out->control_fd >= 0 &&
        out->cli_index > 0 && out->cli_index < argc ? 0 : -1;
}

static int certify_anchor_domain(const struct anchor_arguments *arguments,
    cpl_journal **out_journal, int *out_directory_fd) {
    struct cpl_process_identity identity;
    struct cpl_control_frame frame;
    struct cpl_certified_head certified;
    uint8_t nonce[CPL_HASH_SIZE];
    uint32_t phase = CPL_CONTROL_PHASE_IDENTITY_ACK;
    int status = CPL_ERR_SYSTEM;

    *out_journal = NULL;
    *out_directory_fd = -1;
    if (parse_nonce(arguments->allocation_nonce, nonce) < 0) {
        return -1;
    }
    *out_directory_fd = open(arguments->instance_dir,
        O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (*out_directory_fd < 0) {
        return -1;
    }
    status = cpl_journal_open_at(*out_directory_fd, "allocation.journal",
        *out_directory_fd, "allocation.workdir", nonce, ANCHOR_NORMAL_LIMIT,
        ANCHOR_HARD_LIMIT, out_journal);
    if (status == CPL_OK) {
        status = cpl_process_observe((int64_t)getpid(), &identity);
    }
    if (status == CPL_OK) {
        status = cpl_control_frame_write(arguments->control_fd,
            CPL_CONTROL_ANCHOR_IDENTITY, nonce,
            (const uint8_t *)&identity, (uint32_t)sizeof(identity),
            control_deadline());
    }
    if (status == CPL_OK) {
        status = cpl_control_phase_accept(&phase,
            CPL_CONTROL_ANCHOR_IDENTITY, false);
    }
    if (status == CPL_OK) {
        status = cpl_journal_certify(*out_journal, monotonic_deadline(),
            &certified);
    }
    if (status == CPL_OK) {
        status = cpl_control_frame_read(arguments->control_fd, nonce,
            control_deadline(), &frame);
    }
    if (status == CPL_OK && frame.type != CPL_CONTROL_ANCHOR_ACK) {
        status = CPL_ERR_CONTROL_PHASE;
    }
    if (status == CPL_OK) {
        status = cpl_control_phase_accept(&phase, frame.type, true);
    }
    if (status == CPL_OK) {
        return 0;
    }
    if (*out_journal != NULL) {
        cpl_journal_close(*out_journal);
        *out_journal = NULL;
    }
    (void)close(*out_directory_fd);
    *out_directory_fd = -1;
    return -1;
}

static int arm_cli(const struct anchor_arguments *arguments) {
    struct cpl_process_identity identity;
    struct cpl_control_frame frame;
    uint8_t nonce[CPL_HASH_SIZE];
    uint32_t phase = CPL_CONTROL_PHASE_ANCHOR_ACK;
    int status;

    if (parse_nonce(arguments->allocation_nonce, nonce) < 0) {
        return -1;
    }
    status = cpl_process_observe((int64_t)getpid(), &identity);
    if (status == CPL_OK) {
        status = cpl_control_frame_write(arguments->control_fd,
            CPL_CONTROL_CLI_ARMED, nonce, (const uint8_t *)&identity,
            (uint32_t)sizeof(identity), control_deadline());
    }
    if (status == CPL_OK) {
        status = cpl_control_phase_accept(&phase, CPL_CONTROL_CLI_ARMED,
            false);
    }
    if (status == CPL_OK) {
        status = cpl_control_frame_read(arguments->control_fd, nonce,
            control_deadline(), &frame);
    }
    if (status == CPL_OK && frame.type != CPL_CONTROL_ARMED_ACK) {
        status = CPL_ERR_CONTROL_PHASE;
    }
    if (status == CPL_OK) {
        status = cpl_control_phase_accept(&phase, frame.type, true);
    }
    return status == CPL_OK ? 0 : -1;
}

int main(int argc, char **argv) {
    struct anchor_arguments arguments;
    cpl_journal *journal = NULL;
    char **cli_argv;
    pid_t child;
    int directory_fd = -1;
    int child_status;
    int cli_count;
    int index;

    if (parse_arguments(argc, argv, &arguments) < 0 || setsid() < 0 ||
        getpid() != getpgrp() || getpid() != getsid(0) ||
        certify_anchor_domain(&arguments, &journal, &directory_fd) < 0) {
        _exit(ANCHOR_FAIL_DEAD_EXIT);
    }
    cli_count = argc - arguments.cli_index;
    cli_argv = calloc((size_t)cli_count + 2U, sizeof(*cli_argv));
    if (cli_argv == NULL) {
        cpl_journal_close(journal);
        (void)close(directory_fd);
        _exit(ANCHOR_FAIL_DEAD_EXIT);
    }
    cli_argv[0] = (char *)arguments.real_cli;
    for (index = 0; index < cli_count; ++index) {
        cli_argv[index + 1] = argv[arguments.cli_index + index];
    }
    cli_argv[cli_count + 1] = NULL;
    child = fork();
    if (child < 0) {
        free(cli_argv);
        cpl_journal_close(journal);
        (void)close(directory_fd);
        _exit(ANCHOR_FAIL_DEAD_EXIT);
    }
    if (child == 0) {
        cpl_journal_close(journal);
        (void)close(directory_fd);
        if (arm_cli(&arguments) < 0) {
            _exit(ANCHOR_FAIL_DEAD_EXIT);
        }
        execve(arguments.real_cli, cli_argv, environ);
        _exit(ANCHOR_FAIL_DEAD_EXIT);
    }
    (void)close(arguments.control_fd);
    free(cli_argv);
    if (waitpid(child, &child_status, 0) != child) {
        cpl_journal_close(journal);
        (void)close(directory_fd);
        _exit(ANCHOR_FAIL_DEAD_EXIT);
    }
    cpl_journal_close(journal);
    (void)close(directory_fd);
    if (WIFEXITED(child_status)) {
        return WEXITSTATUS(child_status);
    }
    return ANCHOR_FAIL_DEAD_EXIT;
}
