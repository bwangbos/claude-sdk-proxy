/* Compile real native components with distinct uptime and sleep-inclusive clocks.
 * No processes are launched: only deadline helpers and mutex admission execute.
 */
#include <errno.h>
#include <stdint.h>
#include <time.h>

static time_t simulated_uptime = 100;

static int simulated_clock_gettime(clockid_t clock, struct timespec *value) {
    value->tv_nsec = 0;
    if (clock == CLOCK_UPTIME_RAW) {
        value->tv_sec = simulated_uptime;
        return 0;
    }
    if (clock == CLOCK_MONOTONIC_RAW) {
        value->tv_sec = simulated_uptime + 600;
        return 0;
    }
    errno = EINVAL;
    return -1;
}

#define clock_gettime simulated_clock_gettime
#define main unused_component_main
#if defined(TEST_LIFECYCLE)
#include "../../native/lifecycle.c"
#elif defined(TEST_SUPERVISOR)
#include "../../native/claude_supervisor.c"
#elif defined(TEST_ANCHOR)
#include "../../native/claude_anchor.c"
#else
#error Select a native component
#endif
#undef main
#undef clock_gettime

#define CHECK(condition) do { \
    if (!(condition)) { \
        (void)fprintf(stderr, "clock contract failed at line %d: %s\n", \
            __LINE__, #condition); \
        return 1; \
    } \
} while (0)

int main(void) {
#if defined(TEST_LIFECYCLE)
    pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;

    /* Python's uptime-based future deadline must admit real native work. */
    CHECK(take_mutex(&mutex, 101000000000ULL) == CPL_OK);
    CHECK(pthread_mutex_unlock(&mutex) == 0);
    CHECK(effective_deadline(0U) == 101000000000ULL);
    simulated_uptime = 102;
    CHECK(take_mutex(&mutex, 101000000000ULL) == CPL_ERR_LOCK_TIMEOUT);
    CHECK(pthread_mutex_destroy(&mutex) == 0);
#else
    CHECK(monotonic_deadline() == 101000000000ULL);
    CHECK(control_deadline() == 105000000000ULL);
#if defined(TEST_SUPERVISOR)
    CHECK(lease_deadline() == 130000000000ULL);
    CHECK(selected_lease_deadline(0U) == 130000000000ULL);
    CHECK(selected_lease_deadline(CLEANUP_INJECTION_AFTER_ADMISSION) ==
        100250000000ULL);
#else
    CHECK(!deadline_reached(101000000000ULL));
    simulated_uptime = 102;
    CHECK(deadline_reached(101000000000ULL));
#endif
#endif
    return 0;
}
