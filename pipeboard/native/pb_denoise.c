/* PipeBoard's noise suppression: Speex's preprocessor (libspeexdsp, installed with PipeWire's
 * Pulse side) as a mono LADSPA plugin with a real bypass (owner: a power button on every
 * built-in effect; WebRTC's module has no pass-through). Built once on demand by
 * pipeboard/denoise.py; run in a filter-chain, one per channel.
 *
 * Speex works on 10 ms frames of 16-bit samples: input is buffered a frame, sent in at half level
 * (6 dB of headroom before 16 bits clip, the graph being float) and brought back up; Speex itself
 * is a frame late (measured: the processed sound leaves 20 ms after it came in, the plugin's
 * latency, reported on its "latency" port). The dry sound is delayed by the same two frames, so
 * the bypass is a 20 ms crossfade between two signals in step: bypassed, the output is the input
 * exactly, 20 ms later. Neither headers are installed (libspeexdsp-dev, ladspa-sdk): the few
 * declarations needed are here, from their stable public interfaces. */

#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* ---- Speex preprocessor (speex/speex_preprocess.h) ---- */
typedef struct SpeexPreprocessState_ SpeexPreprocessState;
SpeexPreprocessState *speex_preprocess_state_init(int frame_size, int sampling_rate);
void speex_preprocess_state_destroy(SpeexPreprocessState *st);
int speex_preprocess_run(SpeexPreprocessState *st, int16_t *x);
int speex_preprocess_ctl(SpeexPreprocessState *st, int request, void *ptr);
#define SPEEX_PREPROCESS_SET_DENOISE 0
#define SPEEX_PREPROCESS_SET_AGC 2
#define SPEEX_PREPROCESS_SET_VAD 4
#define SPEEX_PREPROCESS_SET_DEREVERB 8
#define SPEEX_PREPROCESS_SET_NOISE_SUPPRESS 18

/* ---- LADSPA (ladspa.h, version 1.1) ---- */
typedef float LADSPA_Data;
typedef struct { int HintDescriptor; LADSPA_Data LowerBound, UpperBound; } LADSPA_PortRangeHint;
typedef struct LADSPA_Descriptor {
    unsigned long UniqueID;
    const char *Label;
    int Properties;
    const char *Name, *Maker, *Copyright;
    unsigned long PortCount;
    const int *PortDescriptors;
    const char *const *PortNames;
    const LADSPA_PortRangeHint *PortRangeHints;
    void *ImplementationData;
    void *(*instantiate)(const struct LADSPA_Descriptor *, unsigned long);
    void (*connect_port)(void *, unsigned long, LADSPA_Data *);
    void (*activate)(void *);
    void (*run)(void *, unsigned long);
    void (*run_adding)(void *, unsigned long);
    void (*set_run_adding_gain)(void *, LADSPA_Data);
    void (*deactivate)(void *);
    void (*cleanup)(void *);
} LADSPA_Descriptor;
#define IN 0x1
#define OUT 0x2
#define CONTROL 0x4
#define AUDIO 0x8
#define BELOW 0x1
#define ABOVE 0x2
#define TOGGLED 0x4
#define DEFAULT_MIDDLE 0xC0
#define DEFAULT_0 0x200

enum { P_IN, P_OUT, P_BYPASS, P_STRENGTH, P_LATENCY, PORTS };
#define HEADROOM 0.5f      /* into Speex at half level: 6 dB before 16 bits clip */
#define FADE_S 0.02f       /* the bypass crossfade */

typedef struct {
    LADSPA_Data *port[PORTS];
    SpeexPreprocessState *st;
    int frame, pos, strength;
    int16_t *pcm;                 /* the frame being filled, for Speex */
    float *dry, *wet, *dry_mid, *dry_out;  /* this frame's input; the last frame out, wet;
                                              the dry, one and two frames back */
    float mix, step;              /* 0 = processed, 1 = bypassed; per-sample move */
} Denoise;

static void *instantiate(const LADSPA_Descriptor *d, unsigned long rate) {
    (void)d;
    Denoise *p = calloc(1, sizeof *p);
    if (!p) return NULL;
    p->frame = (int)(rate / 100);  /* 10 ms */
    p->st = speex_preprocess_state_init(p->frame, (int)rate);
    p->pcm = calloc(p->frame, sizeof *p->pcm);
    p->dry = calloc(p->frame, sizeof *p->dry);
    p->wet = calloc(p->frame, sizeof *p->wet);
    p->dry_mid = calloc(p->frame, sizeof *p->dry_mid);
    p->dry_out = calloc(p->frame, sizeof *p->dry_out);
    if (!p->st || !p->pcm || !p->dry || !p->wet || !p->dry_mid || !p->dry_out) {
        if (p->st) speex_preprocess_state_destroy(p->st);
        free(p->pcm); free(p->dry); free(p->wet); free(p->dry_mid); free(p->dry_out); free(p);
        return NULL;
    }
    int on = 1, off = 0;
    speex_preprocess_ctl(p->st, SPEEX_PREPROCESS_SET_DENOISE, &on);
    speex_preprocess_ctl(p->st, SPEEX_PREPROCESS_SET_AGC, &off);
    speex_preprocess_ctl(p->st, SPEEX_PREPROCESS_SET_VAD, &off);
    speex_preprocess_ctl(p->st, SPEEX_PREPROCESS_SET_DEREVERB, &off);
    p->strength = 1;  /* not a real value: set on the first run */
    p->step = 1.0f / (FADE_S * (float)rate);
    return p;
}

static void activate(void *h) {  /* a fresh start: empty buffers, settled on the bypass asked */
    Denoise *p = h;
    p->pos = 0;
    memset(p->pcm, 0, p->frame * sizeof *p->pcm);
    memset(p->dry, 0, p->frame * sizeof *p->dry);
    memset(p->wet, 0, p->frame * sizeof *p->wet);
    memset(p->dry_mid, 0, p->frame * sizeof *p->dry_mid);
    memset(p->dry_out, 0, p->frame * sizeof *p->dry_out);
    p->mix = p->port[P_BYPASS] && *p->port[P_BYPASS] > 0.5f ? 1.0f : 0.0f;
}

static void connect_port(void *h, unsigned long port, LADSPA_Data *at) {
    if (port < PORTS) ((Denoise *)h)->port[port] = at;
}

static void process_frame(Denoise *p) {
    speex_preprocess_run(p->st, p->pcm);
    for (int i = 0; i < p->frame; i++) {
        p->wet[i] = (float)p->pcm[i] / (32767.0f * HEADROOM);
        p->dry_out[i] = p->dry_mid[i];
        p->dry_mid[i] = p->dry[i];
    }
}

static void run(void *h, unsigned long n) {
    Denoise *p = h;
    const float *in = p->port[P_IN];
    float *out = p->port[P_OUT];
    int strength = (int)lrintf(*p->port[P_STRENGTH]);
    if (strength != p->strength) {
        p->strength = strength;
        speex_preprocess_ctl(p->st, SPEEX_PREPROCESS_SET_NOISE_SUPPRESS, &strength);
    }
    float target = *p->port[P_BYPASS] > 0.5f ? 1.0f : 0.0f;
    for (unsigned long i = 0; i < n; i++) {
        float x = in[i], v = x * 32767.0f * HEADROOM;
        p->pcm[p->pos] = (int16_t)(v > 32767.0f ? 32767 : v < -32768.0f ? -32768 : lrintf(v));
        p->dry[p->pos] = x;
        if (p->mix < target) p->mix = fminf(target, p->mix + p->step);
        else if (p->mix > target) p->mix = fmaxf(target, p->mix - p->step);
        out[i] = p->wet[p->pos] * (1.0f - p->mix) + p->dry_out[p->pos] * p->mix;
        if (++p->pos == p->frame) {
            process_frame(p);
            p->pos = 0;
        }
    }
    if (p->port[P_LATENCY]) *p->port[P_LATENCY] = (float)(2 * p->frame);
}

static void cleanup(void *h) {
    Denoise *p = h;
    speex_preprocess_state_destroy(p->st);
    free(p->pcm); free(p->dry); free(p->wet); free(p->dry_mid); free(p->dry_out); free(p);
}

static const int descriptors[PORTS] = {
    IN | AUDIO, OUT | AUDIO, IN | CONTROL, IN | CONTROL, OUT | CONTROL};
static const char *const names[PORTS] = {"Input", "Output", "Bypass", "Strength (dB)", "latency"};
static const LADSPA_PortRangeHint hints[PORTS] = {
    {0, 0, 0}, {0, 0, 0}, {TOGGLED | DEFAULT_0, 0, 1},
    {BELOW | ABOVE | DEFAULT_MIDDLE, -40, 0}, {0, 0, 0}};
static const LADSPA_Descriptor descriptor = {
    0x50424e53, "pb_denoise", 0, "PipeBoard Noise Suppression (Speex)", "PipeBoard", "MIT",
    PORTS, descriptors, names, hints, NULL,
    instantiate, connect_port, activate, run, NULL, NULL, NULL, cleanup};

const LADSPA_Descriptor *ladspa_descriptor(unsigned long index) {
    return index == 0 ? &descriptor : NULL;
}
