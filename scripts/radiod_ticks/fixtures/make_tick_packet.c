// Scratch helper: print a radiod status packet carrying a tick group, as hex.
// Regenerate the test fixtures (ka9q-radio pilot-detector head 94d660ee):
//   cd ka9q-radio/src && make status.o pilot.o tick.o tick_status.o
//   gcc -O1 -I. -o /tmp/make_tick_packet \
//       <hf-timestd>/scripts/radiod_ticks/fixtures/make_tick_packet.c \
//       status.o pilot.o tick.o tick_status.o -lm
//   /tmp/make_tick_packet full | incomplete | mask5
// The profile, peak, marker and envelope values are closed-form; the test
// mirrors the formulas.
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "status.h"
#include "tick_status.h"

#define NPROF 40
int main(int argc,char **argv){
  assert(argc == 2);
  struct tick_second *s = calloc(1,sizeof *s);
  int const decim = 2700;
  int64_t const base = 123456789012LL;
  s->second_index = 5000000; s->window_start_index = 5000000 - 240; s->step = 2; s->nprofile = NPROF;
  for(int k=0; k < TICK_NTEMPLATES; k++){
    for(int i=0; i < NPROF; i++)
      s->profile[k][i] = (float)(37.5 * (k+1) * sin(0.01*i)) + I * (float)(-12.25 * cos(0.02*i*(k+1)));
    s->peak_index[k] = s->window_start_index + 10.25 + 7 * k;
    s->peak_snr_db[k] = 20.5f + k;
  }
  for(int m=0; m < TICK_MARKER_TONES; m++) s->marker_energy[m] = 0.125 * (m+1);
  for(int i=0; i < TICK_ENVELOPE_LEN; i++) s->envelope[i] = (int16_t)(i * 31 - 15000);
  s->envelope_scale = 0.00042f;
  s->complete = true;
  unsigned mask = 3; bool pilot = true; int64_t utc = 1478000000000000000LL;
  if(!strcmp(argv[1],"incomplete")){
    tick_second_incomplete(s); s->complete = false; mask = 3; pilot = false;
  } else if(!strcmp(argv[1],"mask5")){
    mask = 5; pilot = false; s->peak_snr_db[TICK_WWV] = 9;
  } else assert(!strcmp(argv[1],"full"));
  uint8_t pkt[16384]; uint8_t *bp = pkt;
  *bp++ = 0;                                  // STATUS
  encode_int(&bp,OUTPUT_SSRC,10000);
  encode_int64(&bp,GPS_TIME,(uint64_t)1478000000123456789LL);
  encode_int64(&bp,INPUT_SAMPLES,(uint64_t)123456789012LL);
  encode_int(&bp,INPUT_SAMPRATE,64800000);
  encode_int(&bp,OUTPUT_SAMPRATE,24000);
  encode_int(&bp,250,7);                      // a type no decoder here knows
  encode_tick_status(&bp,s,utc,pilot,mask,base,decim);
  encode_eol(&bp);
  for(uint8_t *p = pkt; p < bp; p++) printf("%02x",*p);
  printf("\n");
  return 0;
}
