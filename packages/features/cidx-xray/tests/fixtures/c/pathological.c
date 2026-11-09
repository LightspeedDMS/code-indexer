/* pathological.c — deeply nested blocks, long preprocessor chains, edge-case C */

#include <stddef.h>
#include <string.h>

/* Long preprocessor chain */
#define A1 1
#define A2 (A1 + 1)
#define A3 (A2 + 1)
#define A4 (A3 + 1)
#define A5 (A4 + 1)
#define A6 (A5 + 1)
#define A7 (A6 + 1)
#define A8 (A7 + 1)
#define A9 (A8 + 1)
#define A10 (A9 + 1)

/* Multi-level conditional macros */
#if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 199901L
#  define INLINE static inline
#else
#  define INLINE static
#endif

/* Deeply nested typedefs */
typedef int          Int;
typedef Int         *IntPtr;
typedef IntPtr      *IntPtrPtr;
typedef IntPtrPtr   *IntPtrPtrPtr;

/* Deeply nested struct */
typedef struct Inner   { int x; } Inner;
typedef struct Middle  { Inner a; Inner b; int y; } Middle;
typedef struct Outer   { Middle m1; Middle m2; int z; } Outer;
typedef struct Top     { Outer o; char tag[8]; } Top;

/* Deeply nested if/else */
INLINE int classify(int n) {
    if (n < 0) {
        if (n < -100) {
            if (n < -1000) {
                return -3;
            } else {
                return -2;
            }
        } else {
            if (n < -10) {
                return -2;
            } else {
                return -1;
            }
        }
    } else {
        if (n > 100) {
            if (n > 1000) {
                if (n > 10000) {
                    return 4;
                } else {
                    return 3;
                }
            } else {
                return 2;
            }
        } else {
            if (n > 10) {
                return 1;
            } else {
                return 0;
            }
        }
    }
}

/* Nested loops with pointer arithmetic */
INLINE void nested_fill(int *buf, int dim) {
    for (int i = 0; i < dim; i++) {
        for (int j = 0; j < dim; j++) {
            for (int k = 0; k < dim; k++) {
                *(buf + i * dim * dim + j * dim + k) = i + j + k;
            }
        }
    }
}

/* Complex expression with many operators */
INLINE int complex_expr(int a, int b, int c, int d) {
    return (((a + b) * (c - d)) ^ (a & b)) | ((~c) & d) + (a >> 1) + (b << 2);
}

/* Variadic-style macro with stringization and token pasting */
#define STRINGIFY(x) #x
#define CONCAT(a, b) a##b

/* Edge-case: empty struct (GCC extension) */
/* Commented out to stay strictly conformant: struct Empty {}; */

/* Trigraph-free, but lots of escaped characters */
static const char SPECIAL[] = "\t\n\r\\\"\'\0";

/* Function with many params */
static int many_params(int a, int b, int c, int d, int e,
                       int f, int g, int h, int i, int j) {
    return a + b + c + d + e + f + g + h + i + j;
}

/* Recursive depth */
static int fib(int n) {
    if (n <= 1) return n;
    return fib(n - 1) + fib(n - 2);
}

int CONCAT(main, _entry)(void);

int CONCAT(main, _entry)(void) {
    Top t;
    memset(&t, 0, sizeof(t));
    t.o.m1.a.x = A10;
    (void)classify(t.o.m1.a.x);
    (void)complex_expr(1, 2, 3, 4);
    (void)many_params(1,2,3,4,5,6,7,8,9,10);
    (void)fib(10);
    (void)SPECIAL[0];
    return 0;
}

int main(void) {
    return CONCAT(main, _entry)();
}
