#include <stdio.h>
#include <stddef.h>
#include <time.h>

struct shmTime {
    int    mode;
    int    count;
    time_t clockTimeStampSec;
    int    clockTimeStampUSec;
    time_t receiveTimeStampSec;
    int    receiveTimeStampUSec;
    int    leap;
    int    precision;
    int    nsamples;
    int    valid;
    unsigned clockTimeStampNSec;
    unsigned receiveTimeStampNSec;
    int    dummy[8];
};

int main() {
    printf("Size of int: %lu\n", sizeof(int));
    printf("Size of time_t: %lu\n", sizeof(time_t));
    printf("Size of struct shmTime: %lu\n", sizeof(struct shmTime));
    
    printf("Offset mode: %lu\n", offsetof(struct shmTime, mode));
    printf("Offset count: %lu\n", offsetof(struct shmTime, count));
    printf("Offset clockTimeStampSec: %lu\n", offsetof(struct shmTime, clockTimeStampSec));
    printf("Offset clockTimeStampUSec: %lu\n", offsetof(struct shmTime, clockTimeStampUSec));
    printf("Offset receiveTimeStampSec: %lu\n", offsetof(struct shmTime, receiveTimeStampSec));
    printf("Offset receiveTimeStampUSec: %lu\n", offsetof(struct shmTime, receiveTimeStampUSec));
    printf("Offset leap: %lu\n", offsetof(struct shmTime, leap));
    printf("Offset precision: %lu\n", offsetof(struct shmTime, precision));
    printf("Offset nsamples: %lu\n", offsetof(struct shmTime, nsamples));
    printf("Offset valid: %lu\n", offsetof(struct shmTime, valid));
    printf("Offset clockTimeStampNSec: %lu\n", offsetof(struct shmTime, clockTimeStampNSec));
    
    return 0;
}
