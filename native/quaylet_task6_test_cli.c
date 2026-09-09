#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static int write_all(int descriptor, const char *buffer, size_t length) {
    size_t written = 0U;

    while (written < length) {
        ssize_t result = write(descriptor, buffer + written, length - written);

        if (result < 0 && errno == EINTR) {
            continue;
        }
        if (result <= 0) {
            return -1;
        }
        written += (size_t)result;
    }
    return 0;
}

int main(int argc, char **argv) {
    char buffer[4096];
    int output;

    if (argc == 2 && strcmp(argv[1], "--version") == 0) {
        return puts("2.1.251 (Claude Code)") < 0 ? 1 : 0;
    }
    output = open("../stdin.txt", O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC,
        0600);
    if (output < 0) {
        return 1;
    }
    for (;;) {
        ssize_t got = read(STDIN_FILENO, buffer, sizeof(buffer));

        if (got < 0 && errno == EINTR) {
            continue;
        }
        if (got < 0 || (got > 0 && write_all(output, buffer,
                (size_t)got) < 0)) {
            (void)close(output);
            return 1;
        }
        if (got == 0) {
            break;
        }
    }
    return close(output) < 0 ? 1 : 0;
}
