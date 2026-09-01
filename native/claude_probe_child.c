#include <CommonCrypto/CommonDigest.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

extern char **environ;

#define PROBE_EXIT 75
#define PROBE_NAME_CAPACITY 128U
#define PROBE_ENV_CAPACITY 64U

struct environment_entry {
    char name[PROBE_NAME_CAPACITY];
    const char *value;
};

static int compare_entry(const void *left, const void *right) {
    const struct environment_entry *first = left;
    const struct environment_entry *second = right;

    return strcmp(first->name, second->name);
}

static int write_all(int fd, const void *bytes, size_t length) {
    const uint8_t *cursor = bytes;

    while (length > 0U) {
        ssize_t written = write(fd, cursor, length);

        if (written < 0 && errno == EINTR) {
            continue;
        }
        if (written <= 0) {
            return -1;
        }
        cursor += (size_t)written;
        length -= (size_t)written;
    }
    return 0;
}

int main(int argc, char **argv) {
    struct environment_entry entries[PROBE_ENV_CAPACITY];
    char **entry;
    size_t count = 0U;
    size_t index;
    int output_fd;
    bool stubborn = false;
    bool exit_after_write = false;
    bool spawn_descendant = false;
    int descriptor;

    if (argc == 3 && strcmp(argv[1], "--probe-scenario") == 0 &&
        strcmp(argv[2], "confirmed_reap") == 0) {
        for (;;) {
            pause();
        }
    }
    if ((argc != 3 && argc != 4) || strcmp(argv[1], "--output-path") != 0 ||
        (argc == 4 && strcmp(argv[3], "--stubborn") != 0 &&
         strcmp(argv[3], "--exit-after-write") != 0 &&
         strcmp(argv[3], "--spawn-descendant") != 0)) {
        return PROBE_EXIT;
    }
    output_fd = argv[2][0] == '/' ? open(argv[2],
        O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600) : -1;
    stubborn = argc == 4 && strcmp(argv[3], "--stubborn") == 0;
    exit_after_write = argc == 4 &&
        strcmp(argv[3], "--exit-after-write") == 0;
    spawn_descendant = argc == 4 &&
        strcmp(argv[3], "--spawn-descendant") == 0;
    for (descriptor = 3; descriptor < 1024; ++descriptor) {
        int socket_type;
        socklen_t length = (socklen_t)sizeof(socket_type);

        if (fcntl(descriptor, F_GETFD) >= 0 &&
            getsockopt(descriptor, SOL_SOCKET, SO_TYPE, &socket_type,
                &length) == 0) {
            return PROBE_EXIT;
        }
    }
    if (output_fd < 0) {
        return PROBE_EXIT;
    }
    for (entry = environ; *entry != NULL; ++entry) {
        const char *separator = strchr(*entry, '=');
        size_t length;

        if (separator == NULL || count >= PROBE_ENV_CAPACITY) {
            return PROBE_EXIT;
        }
        length = (size_t)(separator - *entry);
        if (length == 0U || length >= sizeof(entries[count].name)) {
            return PROBE_EXIT;
        }
        (void)memcpy(entries[count].name, *entry, length);
        entries[count].name[length] = '\0';
        entries[count].value = separator + 1U;
        ++count;
    }
    qsort(entries, count, sizeof(entries[0]), compare_entry);
    for (index = 0U; index < count; ++index) {
        char line[PROBE_NAME_CAPACITY + 66U];
        unsigned char digest[32];
        size_t cursor = strlen(entries[index].name);
        size_t digest_index;
        static const char hexadecimal[] = "0123456789abcdef";
        (void)CC_SHA256(entries[index].value,
            (CC_LONG)strlen(entries[index].value), digest);
        (void)memcpy(line, entries[index].name, cursor);
        line[cursor++] = '\t';
        for (digest_index = 0U; digest_index < sizeof(digest);
             ++digest_index) {
            line[cursor++] = hexadecimal[digest[digest_index] >> 4U];
            line[cursor++] = hexadecimal[digest[digest_index] & 15U];
        }
        line[cursor++] = '\n';
        if (write_all(output_fd, line, cursor) < 0) {
            return PROBE_EXIT;
        }
    }
    (void)close(output_fd);
    if (exit_after_write) {
        return 0;
    }
    if (spawn_descendant && fork() < 0) {
        return PROBE_EXIT;
    }
    if (stubborn) {
        (void)signal(SIGTERM, SIG_IGN);
    }
    for (;;) {
        pause();
    }
}
