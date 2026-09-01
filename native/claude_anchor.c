#include "lifecycle.h"

#include <CommonCrypto/CommonDigest.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
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

static volatile sig_atomic_t term_seen = 0;

static void term_handler(int signal_number) {
    (void)signal_number;
    term_seen = 1;
}

struct anchor_arguments {
    const char *allocation_nonce;
    const char *instance_dir;
    const char *real_cli;
    int control_fd;
    int fallback_control_fd;
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
    out->fallback_control_fd = -1;
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
        } else if (strcmp(argv[index], "--fallback-control-fd") == 0) {
            if (parse_fd(argv[++index], &out->fallback_control_fd) < 0) {
                return -1;
            }
        } else {
            return -1;
        }
    }
    return out->allocation_nonce != NULL && out->instance_dir != NULL &&
        out->instance_dir[0] == '/' && out->real_cli != NULL &&
        out->real_cli[0] == '/' && out->control_fd >= 0 &&
        out->fallback_control_fd >= 0 &&
        out->cli_index > 0 && out->cli_index < argc ? 0 : -1;
}

static int certify_anchor_domain(const struct anchor_arguments *arguments,
    cpl_journal **out_journal, int *out_directory_fd) {
    struct cpl_process_identity identity;
    struct cpl_control_frame frame;
    struct cpl_bootstrap_head bootstrap;
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
        status = cpl_journal_bootstrap_append(*out_journal,
            CPL_CONTROL_ANCHOR_IDENTITY, (const uint8_t *)&identity,
            (uint32_t)sizeof(identity), control_deadline(), &bootstrap);
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

static int measure_expected_cli(const char *path,
    struct cpl_cli_armed_identity *armed) {
    struct stat metadata;
    CC_SHA256_CTX context;
    uint8_t buffer[4096];
    int fd;

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
    armed->expected_executable_dev = (uint64_t)metadata.st_dev;
    armed->expected_executable_ino = (uint64_t)metadata.st_ino;
    if (CC_SHA256_Final(armed->expected_executable_hash, &context) != 1) {
        return -1;
    }
    (void)CC_SHA256(path, (CC_LONG)strlen(path), armed->expected_path_hash);
    return 0;
}

static int arm_cli(const struct anchor_arguments *arguments) {
    struct cpl_cli_armed_identity armed;
    struct cpl_control_frame frame;
    struct cpl_bootstrap_head bootstrap;
    cpl_journal *journal = NULL;
    uint8_t nonce[CPL_HASH_SIZE];
    uint32_t phase = CPL_CONTROL_PHASE_ANCHOR_ACK;
    int directory_fd = -1;
    int status;

    if (parse_nonce(arguments->allocation_nonce, nonce) < 0) {
        return -1;
    }
    (void)memset(&armed, 0, sizeof(armed));
    directory_fd = open(arguments->instance_dir,
        O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (directory_fd < 0) {
        return -1;
    }
    status = cpl_journal_open_at(directory_fd, "allocation.journal",
        directory_fd, "allocation.workdir", nonce, ANCHOR_NORMAL_LIMIT,
        ANCHOR_HARD_LIMIT, &journal);
    if (status == CPL_OK) {
        status = cpl_process_observe((int64_t)getpid(), &armed.member);
    }
    if (status == CPL_OK && measure_expected_cli(arguments->real_cli,
            &armed) < 0) {
        status = CPL_ERR_PROCESS_IDENTITY;
    }
    if (status == CPL_OK) {
        status = cpl_journal_bootstrap_append(journal,
            CPL_CONTROL_CLI_ARMED, (const uint8_t *)&armed,
            (uint32_t)sizeof(armed), control_deadline(), &bootstrap);
    }
    if (status == CPL_OK) {
        status = cpl_control_frame_write(arguments->control_fd,
            CPL_CONTROL_CLI_ARMED, nonce, (const uint8_t *)&armed,
            (uint32_t)sizeof(armed), control_deadline());
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
    if (journal != NULL) {
        cpl_journal_close(journal);
    }
    (void)close(directory_fd);
    if (status == CPL_OK && fcntl(arguments->control_fd, F_SETFD,
            FD_CLOEXEC) < 0) {
        status = CPL_ERR_SYSTEM;
    }
    if (status == CPL_OK && fcntl(arguments->fallback_control_fd, F_SETFD,
            FD_CLOEXEC) < 0) {
        status = CPL_ERR_SYSTEM;
    }
    return status == CPL_OK ? 0 : -1;
}

static int anchor_control_loop(const struct anchor_arguments *arguments,
    cpl_journal *journal, pid_t child) {
    struct cpl_control_frame frame;
    struct cpl_bootstrap_head bootstrap;
    struct cpl_append_result unconfirmed;
    struct pollfd controls[2];
    uint8_t nonce[CPL_HASH_SIZE];
    uint32_t phase = CPL_CONTROL_PHASE_ARMED_ACK;
    bool running = false;
    bool cleanup = false;
    bool child_reaped = false;
    bool internal_lost = false;
    bool fallback_consumed = false;
    int child_status = 0;

    if (parse_nonce(arguments->allocation_nonce, nonce) < 0) {
        return ANCHOR_FAIL_DEAD_EXIT;
    }
    for (;;) {
        pid_t reaped;

        if (!child_reaped) {
            reaped = waitpid(child, &child_status, WNOHANG);
            if (reaped == child) {
                child_reaped = true;
            } else if (reaped < 0 && errno != EINTR) {
                return ANCHOR_FAIL_DEAD_EXIT;
            }
        }
        if (cleanup && term_seen != 0 && child_reaped) {
            return 0;
        }
        controls[0].fd = internal_lost ? -1 : arguments->control_fd;
        controls[0].events = POLLIN;
        controls[0].revents = 0;
        controls[1].fd = arguments->fallback_control_fd;
        controls[1].events = POLLIN;
        controls[1].revents = 0;
        if (poll(controls, 2U, 10) < 0) {
            if (errno == EINTR) {
                continue;
            }
            return ANCHOR_FAIL_DEAD_EXIT;
        }
        if ((controls[0].revents & POLLIN) != 0) {
            int status = cpl_control_frame_read(arguments->control_fd, nonce,
                control_deadline(), &frame);

            if (status != CPL_OK && running &&
                (controls[0].revents & (POLLHUP | POLLERR)) != 0) {
                internal_lost = true;
                (void)close(arguments->control_fd);
                continue;
            }
            if (status != CPL_OK ||
                cpl_control_phase_accept(&phase, frame.type, false) != CPL_OK) {
                return ANCHOR_FAIL_DEAD_EXIT;
            }
            if (frame.type == CPL_CONTROL_CLI_RUNNING) {
                running = true;
            } else if (frame.type == CPL_CONTROL_CLEANUP_REQUEST) {
                cleanup = true;
            } else {
                return ANCHOR_FAIL_DEAD_EXIT;
            }
        }
        if ((controls[1].revents & POLLIN) != 0) {
            int status = cpl_control_frame_read(
                arguments->fallback_control_fd, nonce, control_deadline(),
                &frame);
            struct cpl_bootstrap_head certified;

            if (status != CPL_OK || !running || !internal_lost ||
                fallback_consumed || frame.payload_length != 0U ||
                frame.type != CPL_CONTROL_SELF_TERM_REQUEST ||
                cpl_control_phase_accept(&phase, frame.type, false) != CPL_OK) {
                continue;
            }
            if (
                cpl_journal_bootstrap_append(journal,
                    CPL_CONTROL_SELF_TERM_REQUEST, frame.payload,
                    frame.payload_length,
                    control_deadline(), &bootstrap) != CPL_OK ||
                cpl_journal_bootstrap_certify(journal,
                    CPL_CONTROL_SELF_TERM_REQUEST, frame.payload,
                    frame.payload_length, control_deadline(),
                    &certified) != CPL_OK ||
                killpg(getpgrp(), SIGTERM) < 0 ||
                cpl_journal_mark_unconfirmed(journal,
                    CPL_UNCONFIRMED_PROOF_UNAVAILABLE,
                    control_deadline(), &unconfirmed) != CPL_OK) {
                return ANCHOR_FAIL_DEAD_EXIT;
            }
            fallback_consumed = true;
        }
        if ((controls[0].revents & (POLLHUP | POLLERR)) != 0 && !running) {
            return ANCHOR_FAIL_DEAD_EXIT;
        }
        if ((controls[0].revents & (POLLHUP | POLLERR)) != 0 && running) {
            internal_lost = true;
            (void)close(arguments->control_fd);
        }
        if ((controls[1].revents & (POLLHUP | POLLERR)) != 0 && running) {
            for (;;) {
                pause();
            }
        }
    }
}

int main(int argc, char **argv) {
    struct anchor_arguments arguments;
    cpl_journal *journal = NULL;
    char **cli_argv;
    pid_t child;
    int release_pipe[2] = {-1, -1};
    int directory_fd = -1;
    int child_status;
    int cli_count;
    int index;
    struct sigaction action;
    struct sigaction ignored;

    (void)memset(&action, 0, sizeof(action));
    action.sa_handler = term_handler;
    (void)sigemptyset(&action.sa_mask);
    (void)memset(&ignored, 0, sizeof(ignored));
    ignored.sa_handler = SIG_IGN;
    (void)sigemptyset(&ignored.sa_mask);
    if (parse_arguments(argc, argv, &arguments) < 0 || setsid() < 0 ||
        getpid() != getpgrp() || getpid() != getsid(0) ||
        sigaction(SIGTERM, &action, NULL) < 0 ||
        sigaction(SIGHUP, &ignored, NULL) < 0 ||
        sigaction(SIGPIPE, &ignored, NULL) < 0 ||
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
    if (pipe(release_pipe) < 0 ||
        fcntl(release_pipe[0], F_SETFD, FD_CLOEXEC) < 0 ||
        fcntl(release_pipe[1], F_SETFD, FD_CLOEXEC) < 0) {
        free(cli_argv);
        cpl_journal_close(journal);
        (void)close(directory_fd);
        _exit(ANCHOR_FAIL_DEAD_EXIT);
    }
    child = fork();
    if (child < 0) {
        (void)close(release_pipe[0]);
        (void)close(release_pipe[1]);
        free(cli_argv);
        cpl_journal_close(journal);
        (void)close(directory_fd);
        _exit(ANCHOR_FAIL_DEAD_EXIT);
    }
    if (child == 0) {
        char released = 'R';

        (void)close(release_pipe[0]);
        cpl_journal_close(journal);
        (void)close(directory_fd);
        if (arm_cli(&arguments) < 0) {
            _exit(ANCHOR_FAIL_DEAD_EXIT);
        }
        while (write(release_pipe[1], &released, 1U) < 0) {
            if (errno != EINTR) {
                _exit(ANCHOR_FAIL_DEAD_EXIT);
            }
        }
        (void)close(release_pipe[1]);
        execve(arguments.real_cli, cli_argv, environ);
        _exit(ANCHOR_FAIL_DEAD_EXIT);
    }
    (void)close(release_pipe[1]);
    for (;;) {
        char released;
        ssize_t got = read(release_pipe[0], &released, 1U);

        if (got < 0 && errno == EINTR) {
            continue;
        }
        if (got != 1 || released != 'R') {
            free(cli_argv);
            (void)close(release_pipe[0]);
            while (waitpid(child, &child_status, 0) < 0 && errno == EINTR) {
            }
            cpl_journal_close(journal);
            (void)close(directory_fd);
            _exit(ANCHOR_FAIL_DEAD_EXIT);
        }
        break;
    }
    (void)close(release_pipe[0]);
    free(cli_argv);
    child_status = anchor_control_loop(&arguments, journal, child);
    (void)close(arguments.control_fd);
    (void)close(arguments.fallback_control_fd);
    cpl_journal_close(journal);
    (void)close(directory_fd);
    return child_status;
}
