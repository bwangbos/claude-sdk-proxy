/*
 * Fail-closed Darwin/APFS lifecycle primitive probe.
 *
 * This executable deliberately reports only capability evidence.  It never
 * prints caller supplied paths or file contents.
 */

#define _DARWIN_C_SOURCE 1

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <libproc.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/attr.h>
#include <sys/file.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/utsname.h>
#include <unistd.h>

enum {
    EXIT_INVOCATION = 64,
    EXIT_UNSUPPORTED = 65,
    EXIT_REQUIRED_SYSCALL = 74,
    CRASH_EXIT_STATUS = 91,
};

enum CrashBoundary {
    CRASH_NONE,
    CRASH_AFTER_CREATE,
    CRASH_AFTER_PREALLOCATE,
    CRASH_AFTER_APPEND,
    CRASH_AFTER_FULLFSYNC,
    CRASH_AFTER_RENAMEAT,
    CRASH_AFTER_UNLINKAT,
    CRASH_AFTER_DIRECTORY_FSYNC,
};

struct RootContext {
    int directory_fd;
    struct stat root_stat;
    struct statfs mount_stat;
    struct utsname uname_info;
    struct timeval boot_time;
    uint32_t root_file_flags;
    unsigned int darwin_major;
};

struct AttrFlags {
    uint32_t length;
    uint32_t flags;
};

struct MountFlagName {
    const char *name;
    uint64_t bit;
};

/* Keep this table sorted by its canonical public flag name. */
static const struct MountFlagName MOUNT_FLAG_NAMES[] = {
#ifdef MNT_ASYNC
    {"async", (uint64_t)(unsigned long)MNT_ASYNC},
#endif
#ifdef MNT_AUTOMOUNTED
    {"automounted", (uint64_t)(unsigned long)MNT_AUTOMOUNTED},
#endif
#ifdef MNT_CPROTECT
    {"cprotect", (uint64_t)(unsigned long)MNT_CPROTECT},
#endif
#ifdef MNT_DEFWRITE
    {"defwrite", (uint64_t)(unsigned long)MNT_DEFWRITE},
#endif
#ifdef MNT_DONTBROWSE
    {"dontbrowse", (uint64_t)(unsigned long)MNT_DONTBROWSE},
#endif
#ifdef MNT_DOVOLFS
    {"dovolfs", (uint64_t)(unsigned long)MNT_DOVOLFS},
#endif
#ifdef MNT_EXPORTED
    {"exported", (uint64_t)(unsigned long)MNT_EXPORTED},
#endif
#ifdef MNT_IGNORE_OWNERSHIP
    {"ignore_ownership", (uint64_t)(unsigned long)MNT_IGNORE_OWNERSHIP},
#endif
#ifdef MNT_JOURNALED
    {"journaled", (uint64_t)(unsigned long)MNT_JOURNALED},
#endif
#ifdef MNT_LOCAL
    {"local", (uint64_t)(unsigned long)MNT_LOCAL},
#endif
#ifdef MNT_MULTILABEL
    {"multilabel", (uint64_t)(unsigned long)MNT_MULTILABEL},
#endif
#ifdef MNT_NOATIME
    {"noatime", (uint64_t)(unsigned long)MNT_NOATIME},
#endif
#ifdef MNT_NODEV
    {"nodev", (uint64_t)(unsigned long)MNT_NODEV},
#endif
#ifdef MNT_NOEXEC
    {"noexec", (uint64_t)(unsigned long)MNT_NOEXEC},
#endif
#ifdef MNT_NOSUID
    {"nosuid", (uint64_t)(unsigned long)MNT_NOSUID},
#endif
#ifdef MNT_NOUSERXATTR
    {"nouserxattr", (uint64_t)(unsigned long)MNT_NOUSERXATTR},
#endif
#ifdef MNT_QUARANTINE
    {"quarantine", (uint64_t)(unsigned long)MNT_QUARANTINE},
#endif
#ifdef MNT_QUOTA
    {"quota", (uint64_t)(unsigned long)MNT_QUOTA},
#endif
#ifdef MNT_RDONLY
    {"rdonly", (uint64_t)(unsigned long)MNT_RDONLY},
#endif
#ifdef MNT_ROOTFS
    {"rootfs", (uint64_t)(unsigned long)MNT_ROOTFS},
#endif
#ifdef MNT_SNAPSHOT
    {"snapshot", (uint64_t)(unsigned long)MNT_SNAPSHOT},
#endif
#ifdef MNT_STRICTATIME
    {"strictatime", (uint64_t)(unsigned long)MNT_STRICTATIME},
#endif
#ifdef MNT_SYNCHRONOUS
    {"synchronous", (uint64_t)(unsigned long)MNT_SYNCHRONOUS},
#endif
#ifdef MNT_UNION
    {"union", (uint64_t)(unsigned long)MNT_UNION},
#endif
};

static size_t mount_flag_count(void) {
    return sizeof(MOUNT_FLAG_NAMES) / sizeof(MOUNT_FLAG_NAMES[0]);
}

static int checked_printf(const char *format, ...) {
    int result = 0;
    va_list arguments;

    va_start(arguments, format);
    result = vprintf(format, arguments);
    va_end(arguments);
    return result < 0 ? -1 : 0;
}

static int checked_putchar(int character) {
    return putchar(character) == EOF ? -1 : 0;
}

static int finish_json_output(void) {
    if (fflush(stdout) == EOF || ferror(stdout)) {
        return -1;
    }
    return 0;
}

static int emit_error(const char *name) {
    if (checked_printf("{\"error\":\"") < 0 ||
        checked_printf("%s", name) < 0 || checked_printf("\"}\n") < 0 ||
        finish_json_output() < 0) {
        return -1;
    }
    return 0;
}

static int emit_json_string(const char *value) {
    const unsigned char *cursor = (const unsigned char *)value;

    if (checked_putchar('"') < 0) {
        return -1;
    }
    while (*cursor != '\0') {
        if (*cursor == '"' || *cursor == '\\') {
            if (checked_putchar('\\') < 0 ||
                checked_putchar((int)*cursor) < 0) {
                return -1;
            }
        } else if (*cursor < 0x20U) {
            if (checked_printf("\\u%04x", (unsigned int)*cursor) < 0) {
                return -1;
            }
        } else {
            if (checked_putchar((int)*cursor) < 0) {
                return -1;
            }
        }
        ++cursor;
    }
    return checked_putchar('"');
}

static uint32_t cloud_placeholder_flags(void) {
    uint32_t flags = 0U;

#ifdef UF_DATALESS
    flags |= (uint32_t)UF_DATALESS;
#endif
#ifdef SF_DATALESS
    flags |= (uint32_t)SF_DATALESS;
#endif
#ifdef UF_DATAVAULT
    flags |= (uint32_t)UF_DATAVAULT;
#endif
    return flags;
}

static int parse_darwin_major(const char *release, unsigned int *major) {
    char *end = NULL;
    unsigned long parsed = 0UL;

    errno = 0;
    parsed = strtoul(release, &end, 10);
    if (errno != 0 || end == release || *end != '.' || parsed > UINT_MAX) {
        return -1;
    }
    *major = (unsigned int)parsed;
    return 0;
}

static int get_root_file_flags(int root_fd, uint32_t *flags) {
    struct attrlist attributes;
    struct AttrFlags received;

    (void)memset(&attributes, 0, sizeof(attributes));
    (void)memset(&received, 0, sizeof(received));
    attributes.bitmapcount = ATTR_BIT_MAP_COUNT;
    attributes.commonattr = ATTR_CMN_FLAGS;
    if (fgetattrlist(root_fd, &attributes, &received, sizeof(received), 0U) < 0) {
        return -1;
    }
    if (received.length < sizeof(received)) {
        errno = EIO;
        return -1;
    }
    *flags = received.flags;
    return 0;
}

static int require_private_regular_file(int fd) {
    struct stat metadata;

    if (fstat(fd, &metadata) < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    if (!S_ISREG(metadata.st_mode) || metadata.st_uid != geteuid() ||
        (metadata.st_mode & ALLPERMS) != 0600) {
        return EXIT_UNSUPPORTED;
    }
    return 0;
}

static bool is_private_runtime_root(const struct stat *metadata) {
    return S_ISDIR(metadata->st_mode) && metadata->st_uid == geteuid() &&
           (metadata->st_mode & ALLPERMS) == 0700;
}

static int revalidate_root_path(
    const char *root, const struct stat *bound_root_stat
) {
    struct stat observed;

    if (lstat(root, &observed) < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    if (!is_private_runtime_root(&observed) ||
        observed.st_dev != bound_root_stat->st_dev ||
        observed.st_ino != bound_root_stat->st_ino) {
        return EXIT_UNSUPPORTED;
    }
    return 0;
}

static int prepare_root(const char *root, struct RootContext *context) {
    struct stat initial_stat;
    struct proc_bsdinfo process_info;
    int status = 0;
    size_t boot_size = sizeof(context->boot_time);

    (void)memset(context, 0, sizeof(*context));
    context->directory_fd = -1;
    if (lstat(root, &initial_stat) < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    if (!is_private_runtime_root(&initial_stat)) {
        return EXIT_UNSUPPORTED;
    }

    context->directory_fd = open(
        root, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC
    );
    if (context->directory_fd < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    if (fstat(context->directory_fd, &context->root_stat) < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    if (!is_private_runtime_root(&context->root_stat) ||
        context->root_stat.st_dev != initial_stat.st_dev ||
        context->root_stat.st_ino != initial_stat.st_ino) {
        return EXIT_UNSUPPORTED;
    }

    if (fstatfs(context->directory_fd, &context->mount_stat) < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    if (strcmp(context->mount_stat.f_fstypename, "apfs") != 0 ||
        (context->mount_stat.f_flags & MNT_LOCAL) == 0 ||
        context->mount_stat.f_mntfromname[0] == '\0') {
        return EXIT_UNSUPPORTED;
    }

    if (uname(&context->uname_info) < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    if (strcmp(context->uname_info.sysname, "Darwin") != 0 ||
        parse_darwin_major(context->uname_info.release, &context->darwin_major) != 0 ||
        context->darwin_major < 23U) {
        return EXIT_UNSUPPORTED;
    }
    if (sysctlbyname(
            "kern.boottime", &context->boot_time, &boot_size, NULL, 0U
        ) < 0 ||
        boot_size != sizeof(context->boot_time) || context->boot_time.tv_sec < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    if (get_root_file_flags(context->directory_fd, &context->root_file_flags) < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    if ((context->root_file_flags & cloud_placeholder_flags()) != 0U) {
        return EXIT_UNSUPPORTED;
    }
    if (proc_pidinfo(
            getpid(), PROC_PIDTBSDINFO, 0U, &process_info, sizeof(process_info)
        ) != (int)sizeof(process_info)) {
        return EXIT_REQUIRED_SYSCALL;
    }
    status = revalidate_root_path(root, &context->root_stat);
    return status;
}

static void close_root(struct RootContext *context) {
    if (context->directory_fd >= 0) {
        (void)close(context->directory_fd);
        context->directory_fd = -1;
    }
}

static int fullfsync_file(int fd) {
    if (fcntl(fd, F_FULLFSYNC) < 0) {
        return -1;
    }
    return 0;
}

static int preallocate_file(int fd, off_t length) {
    struct fstore allocation;

    (void)memset(&allocation, 0, sizeof(allocation));
    allocation.fst_flags = F_ALLOCATECONTIG;
    allocation.fst_posmode = F_PEOFPOSMODE;
    allocation.fst_length = length;
    if (fcntl(fd, F_PREALLOCATE, &allocation) == 0) {
        return 0;
    }
    allocation.fst_flags = F_ALLOCATEALL;
    return fcntl(fd, F_PREALLOCATE, &allocation);
}

static int write_all(int fd, const unsigned char *data, size_t length) {
    size_t written = 0U;

    while (written < length) {
        ssize_t result = write(fd, data + written, length - written);

        if (result < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        if (result == 0) {
            errno = EIO;
            return -1;
        }
        written += (size_t)result;
    }
    return 0;
}

static int create_private_file(int directory_fd, const char *name) {
    int fd = openat(
        directory_fd,
        name,
        O_WRONLY | O_CREAT | O_EXCL | O_APPEND | O_NOFOLLOW | O_CLOEXEC,
        0600
    );
    int validity = 0;

    if (fd < 0) {
        return -1;
    }
    validity = require_private_regular_file(fd);
    if (validity != 0) {
        (void)close(fd);
        errno = EPERM;
        return -1;
    }
    return fd;
}

static void crash_at(enum CrashBoundary expected, enum CrashBoundary current) {
    if (expected == current) {
        _exit(CRASH_EXIT_STATUS);
    }
}

static int lifecycle_file_sequence(
    int directory_fd,
    const char *current_name,
    const char *renamed_name,
    enum CrashBoundary crash_boundary
) {
    static const unsigned char RECORD[] = "darwin-probe-record";
    int fd = -1;

    fd = create_private_file(directory_fd, current_name);
    if (fd < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    crash_at(crash_boundary, CRASH_AFTER_CREATE);
    if (preallocate_file(fd, 4096) < 0) {
        (void)close(fd);
        return EXIT_REQUIRED_SYSCALL;
    }
    crash_at(crash_boundary, CRASH_AFTER_PREALLOCATE);
    if (write_all(fd, RECORD, sizeof(RECORD) - 1U) < 0) {
        (void)close(fd);
        return EXIT_REQUIRED_SYSCALL;
    }
    crash_at(crash_boundary, CRASH_AFTER_APPEND);
    if (fullfsync_file(fd) < 0) {
        (void)close(fd);
        return EXIT_REQUIRED_SYSCALL;
    }
    crash_at(crash_boundary, CRASH_AFTER_FULLFSYNC);
    if (close(fd) < 0 ||
        renameat(directory_fd, current_name, directory_fd, renamed_name) < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    crash_at(crash_boundary, CRASH_AFTER_RENAMEAT);
    if (unlinkat(directory_fd, renamed_name, 0) < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    crash_at(crash_boundary, CRASH_AFTER_UNLINKAT);
    if (fsync(directory_fd) < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    crash_at(crash_boundary, CRASH_AFTER_DIRECTORY_FSYNC);
    return 0;
}

static uint64_t known_mount_flag_bits(void) {
    uint64_t known = 0U;
    size_t index = 0U;

    for (index = 0U; index < mount_flag_count(); ++index) {
        known |= MOUNT_FLAG_NAMES[index].bit;
    }
    return known;
}

static int emit_mount_flags(uint64_t flags) {
    size_t index = 0U;
    bool first = true;

    if ((flags & ~known_mount_flag_bits()) != 0U) {
        return -1;
    }
    if (checked_putchar('[') < 0) {
        return -1;
    }
    for (index = 0U; index < mount_flag_count(); ++index) {
        if ((flags & MOUNT_FLAG_NAMES[index].bit) == 0U) {
            continue;
        }
        if (!first) {
            if (checked_putchar(',') < 0) {
                return -1;
            }
        }
        if (emit_json_string(MOUNT_FLAG_NAMES[index].name) < 0) {
            return -1;
        }
        first = false;
    }
    return checked_putchar(']');
}

static int command_platform(const char *root) {
    struct RootContext context;
    uint32_t fsid_first = 0U;
    uint32_t fsid_second = 0U;
    int status = prepare_root(root, &context);

    if (status != 0) {
        close_root(&context);
        return status;
    }
    status = lifecycle_file_sequence(
        context.directory_fd,
        ".darwin-probe-platform-current",
        ".darwin-probe-platform-renamed",
        CRASH_NONE
    );
    if (status != 0) {
        close_root(&context);
        return status;
    }
    if (((uint64_t)(unsigned long)context.mount_stat.f_flags &
         ~known_mount_flag_bits()) != 0U) {
        close_root(&context);
        return EXIT_UNSUPPORTED;
    }

    fsid_first = (uint32_t)context.mount_stat.f_fsid.val[0];
    fsid_second = (uint32_t)context.mount_stat.f_fsid.val[1];
    if (checked_printf(
            "{\"darwin_major\":%u,\"filesystem_type\":\"apfs\",",
            context.darwin_major
        ) < 0 ||
        checked_printf("\"is_local\":true,\"mount_device\":") < 0 ||
        emit_json_string(context.mount_stat.f_mntfromname) < 0 ||
        checked_printf(
            ",\"mount_fsid\":\"%08" PRIx32 ":%08" PRIx32
            "\",\"mount_flags\":",
            fsid_first,
            fsid_second
        ) < 0 ||
        emit_mount_flags((uint64_t)(unsigned long)context.mount_stat.f_flags) <
            0 ||
        checked_printf(
            ",\"runtime_root_st_dev\":%ju",
            (uintmax_t)context.root_stat.st_dev
        ) < 0 ||
        checked_printf(",\"os_build\":") < 0 ||
        emit_json_string(context.uname_info.version) < 0 ||
        checked_printf(",\"boot_time\":%ju", (uintmax_t)context.boot_time.tv_sec) <
            0 ||
        checked_printf(
            ",\"preallocate\":true,\"fullfsync_file\":true"
            ",\"renameat\":true,\"unlinkat\":true"
            ",\"fsync_directory\":true,\"proc_pidinfo\":true}\n"
        ) < 0 ||
        finish_json_output() < 0) {
        close_root(&context);
        return EXIT_REQUIRED_SYSCALL;
    }
    close_root(&context);
    return 0;
}

static enum CrashBoundary parse_crash_boundary(const char *name) {
    if (strcmp(name, "after_create") == 0) {
        return CRASH_AFTER_CREATE;
    }
    if (strcmp(name, "after_preallocate") == 0) {
        return CRASH_AFTER_PREALLOCATE;
    }
    if (strcmp(name, "after_append") == 0) {
        return CRASH_AFTER_APPEND;
    }
    if (strcmp(name, "after_fullfsync") == 0) {
        return CRASH_AFTER_FULLFSYNC;
    }
    if (strcmp(name, "after_renameat") == 0) {
        return CRASH_AFTER_RENAMEAT;
    }
    if (strcmp(name, "after_unlinkat") == 0) {
        return CRASH_AFTER_UNLINKAT;
    }
    if (strcmp(name, "after_directory_fsync") == 0) {
        return CRASH_AFTER_DIRECTORY_FSYNC;
    }
    return CRASH_NONE;
}

static int emit_crash_prefix(enum CrashBoundary boundary) {
    static const char *const PREFIX[] = {
        "create",
        "preallocate",
        "append",
        "fullfsync",
        "renameat",
        "unlinkat",
        "directory_fsync",
    };
    size_t count = 0U;
    size_t index = 0U;

    if (boundary >= CRASH_AFTER_FULLFSYNC) {
        count = 4U;
    }
    if (boundary == CRASH_AFTER_DIRECTORY_FSYNC) {
        count = sizeof(PREFIX) / sizeof(PREFIX[0]);
    }
    if (checked_putchar('[') < 0) {
        return -1;
    }
    for (index = 0U; index < count; ++index) {
        if (index != 0U) {
            if (checked_putchar(',') < 0) {
                return -1;
            }
        }
        if (emit_json_string(PREFIX[index]) < 0) {
            return -1;
        }
    }
    return checked_putchar(']');
}

static int command_crash(const char *root, const char *boundary_name) {
    struct RootContext context;
    enum CrashBoundary boundary = parse_crash_boundary(boundary_name);
    int status = 0;
    int wait_status = 0;
    pid_t child = 0;

    if (boundary == CRASH_NONE) {
        return EXIT_INVOCATION;
    }
    status = prepare_root(root, &context);
    if (status != 0) {
        close_root(&context);
        return status;
    }
    child = fork();
    if (child < 0) {
        close_root(&context);
        return EXIT_REQUIRED_SYSCALL;
    }
    if (child == 0) {
        int child_status = lifecycle_file_sequence(
            context.directory_fd,
            ".darwin-probe-crash-current",
            ".darwin-probe-crash-renamed",
            boundary
        );

        _exit(child_status == 0 ? EXIT_REQUIRED_SYSCALL : child_status);
    }
    if (waitpid(child, &wait_status, 0) < 0 || !WIFEXITED(wait_status) ||
        WEXITSTATUS(wait_status) != CRASH_EXIT_STATUS) {
        close_root(&context);
        return EXIT_REQUIRED_SYSCALL;
    }
    if (checked_printf("{\"crash_boundary\":") < 0 ||
        emit_json_string(boundary_name) < 0 ||
        checked_printf(
            ",\"child_exit_status\":%d,\"authorized_prefix\":",
            CRASH_EXIT_STATUS
        ) < 0 ||
        emit_crash_prefix(boundary) < 0 || checked_printf("}\n") < 0 ||
        finish_json_output() < 0) {
        close_root(&context);
        return EXIT_REQUIRED_SYSCALL;
    }
    close_root(&context);
    return 0;
}

static int read_exactly_one(int fd) {
    unsigned char value = 0U;

    for (;;) {
        ssize_t result = read(fd, &value, sizeof(value));

        if (result == (ssize_t)sizeof(value)) {
            return 0;
        }
        if (result < 0 && errno == EINTR) {
            continue;
        }
        return -1;
    }
}

static int write_exactly_one(int fd) {
    const unsigned char value = 1U;

    for (;;) {
        ssize_t result = write(fd, &value, sizeof(value));

        if (result == (ssize_t)sizeof(value)) {
            return 0;
        }
        if (result < 0 && errno == EINTR) {
            continue;
        }
        return -1;
    }
}

static int open_canonical_lock(int directory_fd, const char *name) {
    int fd = openat(
        directory_fd, name, O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0600
    );
    int validity = 0;

    if (fd < 0) {
        return -1;
    }
    validity = require_private_regular_file(fd);
    if (validity != 0) {
        (void)close(fd);
        errno = EPERM;
        return -1;
    }
    return fd;
}

static int prove_lock_lifecycle(int directory_fd, const char *name) {
    int child_ready[2] = {-1, -1};
    int child_release[2] = {-1, -1};
    int contender_fd = -1;
    int released_fd = -1;
    int wait_status = 0;
    pid_t child = 0;

    if (pipe(child_ready) < 0 || pipe(child_release) < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    child = fork();
    if (child < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    if (child == 0) {
        int held_fd = -1;

        (void)close(child_ready[0]);
        (void)close(child_release[1]);
        held_fd = open_canonical_lock(directory_fd, name);
        if (held_fd < 0 || flock(held_fd, LOCK_EX | LOCK_NB) < 0 ||
            write_exactly_one(child_ready[1]) < 0 ||
            read_exactly_one(child_release[0]) < 0) {
            _exit(EXIT_REQUIRED_SYSCALL);
        }
        _exit(0);
    }

    (void)close(child_ready[1]);
    (void)close(child_release[0]);
    if (read_exactly_one(child_ready[0]) < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    contender_fd = open_canonical_lock(directory_fd, name);
    if (contender_fd < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    if (flock(contender_fd, LOCK_EX | LOCK_NB) == 0 ||
        (errno != EWOULDBLOCK && errno != EAGAIN)) {
        (void)close(contender_fd);
        return EXIT_REQUIRED_SYSCALL;
    }
    (void)close(contender_fd);
    if (write_exactly_one(child_release[1]) < 0 ||
        waitpid(child, &wait_status, 0) < 0 || !WIFEXITED(wait_status) ||
        WEXITSTATUS(wait_status) != 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    released_fd = open_canonical_lock(directory_fd, name);
    if (released_fd < 0 || flock(released_fd, LOCK_EX | LOCK_NB) < 0) {
        if (released_fd >= 0) {
            (void)close(released_fd);
        }
        return EXIT_REQUIRED_SYSCALL;
    }
    (void)close(released_fd);
    return 0;
}

static int command_lock(const char *root, const char *name, const char *field) {
    struct RootContext context;
    int status = prepare_root(root, &context);

    if (status != 0) {
        close_root(&context);
        return status;
    }
    status = prove_lock_lifecycle(context.directory_fd, name);
    close_root(&context);
    if (status != 0) {
        return status;
    }
    if (checked_printf("{\"") < 0 || checked_printf("%s", field) < 0 ||
        checked_printf("\":true,\"process_exit_release\":true}\n") < 0 ||
        finish_json_output() < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    return 0;
}

static int create_owner_record(int directory_fd) {
    static const unsigned char RECORD[] = "owner-record-create";
    int fd = create_private_file(directory_fd, "owner.record");

    if (fd < 0 || write_all(fd, RECORD, sizeof(RECORD) - 1U) < 0 ||
        fullfsync_file(fd) < 0 || close(fd) < 0 || fsync(directory_fd) < 0) {
        if (fd >= 0) {
            (void)close(fd);
        }
        return EXIT_REQUIRED_SYSCALL;
    }
    return 0;
}

static int command_owner_record_create(const char *root) {
    struct RootContext context;
    int status = prepare_root(root, &context);

    if (status != 0) {
        close_root(&context);
        return status;
    }
    status = create_owner_record(context.directory_fd);
    close_root(&context);
    if (status != 0) {
        return status;
    }
    if (checked_printf("{\"owner_record_create\":true}\n") < 0 ||
        finish_json_output() < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    return 0;
}

static int command_owner_record_replace(const char *root) {
    static const unsigned char REPLACEMENT[] = "owner-record-replace";
    struct RootContext context;
    int replacement_fd = -1;
    int status = prepare_root(root, &context);

    if (status != 0) {
        close_root(&context);
        return status;
    }
    status = create_owner_record(context.directory_fd);
    if (status != 0) {
        close_root(&context);
        return status;
    }
    replacement_fd = create_private_file(context.directory_fd, ".owner.record.tmp");
    if (replacement_fd < 0 ||
        write_all(
            replacement_fd, REPLACEMENT, sizeof(REPLACEMENT) - 1U
        ) < 0 ||
        fullfsync_file(replacement_fd) < 0 || close(replacement_fd) < 0 ||
        renameat(
            context.directory_fd,
            ".owner.record.tmp",
            context.directory_fd,
            "owner.record"
        ) < 0 ||
        fsync(context.directory_fd) < 0) {
        if (replacement_fd >= 0) {
            (void)close(replacement_fd);
        }
        close_root(&context);
        return EXIT_REQUIRED_SYSCALL;
    }
    close_root(&context);
    if (checked_printf("{\"owner_record_replace\":true}\n") < 0 ||
        finish_json_output() < 0) {
        return EXIT_REQUIRED_SYSCALL;
    }
    return 0;
}

int main(int argc, char *argv[]) {
    int status = EXIT_INVOCATION;

    if (argc == 3 && strcmp(argv[1], "platform") == 0) {
        status = command_platform(argv[2]);
    } else if (argc == 4 && strcmp(argv[1], "crash") == 0) {
        status = command_crash(argv[2], argv[3]);
    } else if (argc == 3 && strcmp(argv[1], "root_reconciliation_lock") == 0) {
        status = command_lock(
            argv[2], ".root-reconciliation.lock", "root_reconciliation_lock"
        );
    } else if (argc == 3 && strcmp(argv[1], "instance_lifetime_lock") == 0) {
        status = command_lock(
            argv[2], ".instance-lifetime.lock", "instance_lifetime_lock"
        );
    } else if (argc == 3 && strcmp(argv[1], "owner_record_create") == 0) {
        status = command_owner_record_create(argv[2]);
    } else if (argc == 3 && strcmp(argv[1], "owner_record_replace") == 0) {
        status = command_owner_record_replace(argv[2]);
    }

    if (status == 0) {
        return 0;
    }
    if (status == EXIT_INVOCATION) {
        if (emit_error("invocation") < 0) {
            return EXIT_REQUIRED_SYSCALL;
        }
    } else if (status == EXIT_UNSUPPORTED) {
        if (emit_error("unsupported") < 0) {
            return EXIT_REQUIRED_SYSCALL;
        }
    } else {
        if (emit_error("required_syscall") < 0) {
            return EXIT_REQUIRED_SYSCALL;
        }
        status = EXIT_REQUIRED_SYSCALL;
    }
    return status;
}
