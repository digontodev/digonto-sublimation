/*
 * dtfcore.c - fast inner loops for DTF-RIP
 *
 *  - multi-level error diffusion halftoning (serpentine Floyd-Steinberg)
 *  - building ESC/P2 raster blocks (2-bit pack + mask + PackBits RLE)
 *
 * Built at install time:  gcc -O3 -shared -fPIC -o libdtfcore.so dtfcore.c
 */
#include <stdint.h>
#include <string.h>
#include <math.h>

/* ---------------------------------------------------------------- */
/* Error diffusion                                                   */
/* ---------------------------------------------------------------- */

static inline uint32_t lcg(uint32_t *s) { *s = *s * 1664525u + 1013904223u; return *s; }

/*
 * in      : float rows, values 0..1 (ink amount, 1.0 = every pixel a large dot)
 * out     : uint8 rows, dot level 0..nlev-1 mapped through level_codes
 * err     : persistent state, 2*(w+4) floats (current row / next row)
 * levels  : ink value of each output level (levels[0] must be 0)
 * codes   : 2-bit code written for each level (0 = no dot, 1 small, 2 medium, 3 large)
 * row0    : absolute row number of the first row (serpentine parity)
 * mirror  : 1 = scan the row mirrored (gives a different pattern for 2nd white)
 * noise   : threshold jitter amplitude (0..0.1), breaks up "worm" artefacts
 */
void ed_strip(const float *in, int w, int rows, int stride_in,
              uint8_t *out, int stride_out,
              float *err, const float *levels, const uint8_t *codes, int nlev,
              int row0, int mirror, float noise, uint32_t seed)
{
    float *cur = err;
    float *nxt = err + (w + 4);
    uint32_t rs = seed ^ (uint32_t)(row0 * 2654435761u);
    for (int r = 0; r < rows; r++) {
        const float *src = in + (size_t)r * stride_in;
        uint8_t *dst = out + (size_t)r * stride_out;
        int ltr = ((row0 + r) & 1) == 0;
        if (mirror) ltr = !ltr;
        memset(nxt, 0, sizeof(float) * (w + 4));
        for (int i = 0; i < w; i++) {
            int x = ltr ? i : (w - 1 - i);
            int px = mirror ? (w - 1 - x) : x;  /* pixel position in image */
            float v = src[px];
            if (v <= 0.0f && cur[x + 2] <= 0.0f) { dst[px] = 0; continue; }
            float val = v + cur[x + 2];
            float jitter = 0.0f;
            if (noise > 0.0f)
                jitter = ((float)(lcg(&rs) >> 8) / 16777216.0f - 0.5f) * noise;
            /* nearest level, with jittered decision boundaries */
            int best = 0;
            for (int k = nlev - 1; k > 0; k--) {
                float mid = 0.5f * (levels[k] + levels[k - 1]) + jitter;
                if (val >= mid) { best = k; break; }
            }
            if (v <= 0.0f) best = 0;          /* never put ink where there is none */
            dst[px] = codes[best];
            float e = val - levels[best];
            /* clamp error so huge negative errors do not starve neighbours */
            if (e > 1.0f) e = 1.0f; else if (e < -1.0f) e = -1.0f;
            int d = ltr ? 1 : -1;
            cur[x + 2 + d] += e * (7.0f / 16.0f);
            nxt[x + 2 - d] += e * (3.0f / 16.0f);
            nxt[x + 2]     += e * (5.0f / 16.0f);
            nxt[x + 2 + d] += e * (1.0f / 16.0f);
        }
        /* next row's error becomes the current row (state lives in first half) */
        memcpy(cur, nxt, sizeof(float) * (w + 4));
    }
}

/* ---------------------------------------------------------------- */
/* PackBits                                                          */
/* ---------------------------------------------------------------- */

int packbits(const uint8_t *src, int n, uint8_t *dst)
{
    int i = 0, o = 0;
    while (i < n) {
        /* run? */
        int run = 1;
        while (i + run < n && run < 128 && src[i + run] == src[i]) run++;
        if (run >= 3 || (run == 2 && i + run == n)) {
            dst[o++] = (uint8_t)(257 - run);
            dst[o++] = src[i];
            i += run;
            continue;
        }
        /* literal stretch until a run of 3 starts */
        int start = i, lit = 0;
        while (i < n && lit < 128) {
            if (i + 2 < n && src[i] == src[i + 1] && src[i] == src[i + 2]) break;
            i++; lit++;
        }
        dst[o++] = (uint8_t)(lit - 1);
        memcpy(dst + o, src + start, lit);
        o += lit;
    }
    return o;
}

/* ---------------------------------------------------------------- */
/* Raster block for one ESC i command                                */
/* ---------------------------------------------------------------- */

/*
 * plane     : dot codes (0..3), one byte per device pixel, row-major
 * row_idx   : for every nozzle row 0..nrows-1 the image row to print, or -1
 * phase,H   : print columns x = phase, phase+H, ...  (horizontal interleave)
 * ncols     : pixels per nozzle row (ceil(width / H))
 * mask      : 64x64 bytes; pixel printed only if mask % K == m
 * out       : RLE output buffer
 * rows_used : trailing empty rows are not sent
 * returns   : bytes written, 0 if the block is completely empty
 */
int build_block(const uint8_t *plane, int width, int height, int stride,
                const int *row_idx, int nrows, int phase, int H, int ncols,
                const uint8_t *mask, int K, int m,
                uint8_t *out, int *rows_used, uint8_t *tmp_row)
{
    int nb = (ncols + 3) / 4;
    int last_nonempty = -1;
    int o = 0;
    /* first pass: find the last nozzle row with ink */
    for (int j = 0; j < nrows; j++) {
        int y = row_idx[j];
        if (y < 0 || y >= height) continue;
        const uint8_t *src = plane + (size_t)y * stride;
        const uint8_t *mrow = mask + (y & 63) * 64;
        for (int i = 0; i < ncols; i++) {
            int x = phase + i * H;
            if (x >= width) break;
            if (src[x] && (K <= 1 || (mrow[i & 63] % K) == m)) { last_nonempty = j; break; }
        }
    }
    *rows_used = last_nonempty + 1;
    if (last_nonempty < 0) return 0;
    for (int j = 0; j <= last_nonempty; j++) {
        int y = row_idx[j];
        memset(tmp_row, 0, nb);
        if (y >= 0 && y < height) {
            const uint8_t *src = plane + (size_t)y * stride;
            const uint8_t *mrow = mask + (y & 63) * 64;
            for (int i = 0; i < ncols; i++) {
                int x = phase + i * H;
                if (x >= width) break;
                uint8_t c = src[x];
                if (!c) continue;
                if (K > 1 && (mrow[i & 63] % K) != m) continue;
                tmp_row[i >> 2] |= (uint8_t)(c << (6 - 2 * (i & 3)));
            }
        }
        o += packbits(tmp_row, nb, out + o);
    }
    return o;
}

/* ---------------------------------------------------------------- */
/* Morphology helper: square erosion of a uint8 mask (white choke)    */
/* ---------------------------------------------------------------- */
void erode_rows(uint8_t *img, int w, int h, int r, uint8_t *tmp)
{
    if (r <= 0) return;
    /* horizontal */
    for (int y = 0; y < h; y++) {
        uint8_t *row = img + (size_t)y * w;
        memcpy(tmp, row, w);
        for (int x = 0; x < w; x++) {
            uint8_t mn = 255;
            int a = x - r < 0 ? 0 : x - r, b = x + r >= w ? w - 1 : x + r;
            if (x - r < 0 || x + r >= w) mn = 0;  /* outside = transparent */
            for (int k = a; k <= b && mn; k++) if (tmp[k] < mn) mn = tmp[k];
            row[x] = mn;
        }
    }
    /* vertical */
    for (int x = 0; x < w; x++) {
        for (int y = 0; y < h; y++) tmp[y] = img[(size_t)y * w + x];
        for (int y = 0; y < h; y++) {
            uint8_t mn = 255;
            int a = y - r < 0 ? 0 : y - r, b = y + r >= h ? h - 1 : y + r;
            if (y - r < 0 || y + r >= h) mn = 0;
            for (int k = a; k <= b && mn; k++) if (tmp[k] < mn) mn = tmp[k];
            img[(size_t)y * w + x] = mn;
        }
    }
}
