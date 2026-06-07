/* advanced.c — pointers, function pointers, macros, nested structs, switch, goto */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Macro utilities */
#define ARRAY_LEN(a) (sizeof(a) / sizeof((a)[0]))
#define MIN(a, b)    ((a) < (b) ? (a) : (b))
#define SWAP(T, a, b) do { T _t = (a); (a) = (b); (b) = _t; } while (0)

/* Nested struct: address inside person */
typedef struct {
    char street[64];
    char city[32];
    int  zip;
} Address;

typedef struct {
    char    name[32];
    int     age;
    Address addr;
} Person;

/* Function pointer typedef for comparators */
typedef int (*CmpFn)(const void *, const void *);

/* Generic selection sort using a function pointer comparator */
void selection_sort(void *base, size_t nmemb, size_t size, CmpFn cmp) {
    char *arr = (char *)base;
    for (size_t i = 0; i < nmemb - 1; i++) {
        size_t min_idx = i;
        for (size_t j = i + 1; j < nmemb; j++) {
            if (cmp(arr + j * size, arr + min_idx * size) < 0) {
                min_idx = j;
            }
        }
        if (min_idx != i) {
            /* Swap elements byte-by-byte */
            char *a = arr + i * size;
            char *b = arr + min_idx * size;
            for (size_t k = 0; k < size; k++) {
                SWAP(char, a[k], b[k]);
            }
        }
    }
}

static int cmp_int(const void *a, const void *b) {
    return *(const int *)a - *(const int *)b;
}

/* State machine using switch + goto for error path */
typedef enum { STATE_INIT, STATE_RUNNING, STATE_PAUSED, STATE_DONE, STATE_ERROR } State;

const char *state_name(State s) {
    switch (s) {
        case STATE_INIT:    return "INIT";
        case STATE_RUNNING: return "RUNNING";
        case STATE_PAUSED:  return "PAUSED";
        case STATE_DONE:    return "DONE";
        case STATE_ERROR:   return "ERROR";
        default:            return "UNKNOWN";
    }
}

int run_machine(int *steps, int n) {
    if (steps == NULL || n <= 0) {
        goto error;
    }

    State state = STATE_INIT;
    for (int i = 0; i < n; i++) {
        switch (steps[i]) {
            case 0: state = STATE_INIT;    break;
            case 1: state = STATE_RUNNING; break;
            case 2: state = STATE_PAUSED;  break;
            case 3: state = STATE_DONE;    return 0;
            default:
                goto error;
        }
        printf("step %d: state=%s\n", i, state_name(state));
    }
    return 0;

error:
    fprintf(stderr, "machine error\n");
    return -1;
}

/* Pointer arithmetic over a flat matrix */
void matrix_fill(int *m, int rows, int cols) {
    for (int r = 0; r < rows; r++) {
        for (int c = 0; c < cols; c++) {
            *(m + r * cols + c) = r * cols + c;
        }
    }
}

int main(void) {
    /* Function pointer array dispatch */
    CmpFn comparators[] = { cmp_int };
    int nums[] = { 5, 3, 8, 1, 9, 2 };
    selection_sort(nums, ARRAY_LEN(nums), sizeof(int), comparators[0]);
    for (size_t i = 0; i < ARRAY_LEN(nums); i++) {
        printf("%d ", nums[i]);
    }
    printf("\n");

    int steps[] = { 1, 2, 1, 3 };
    run_machine(steps, (int)ARRAY_LEN(steps));

    int matrix[3][4];
    matrix_fill(&matrix[0][0], 3, 4);

    Person p = { "Alice", 30, { "123 Main St", "Anytown", 12345 } };
    printf("%s lives at %s, %s\n", p.name, p.addr.street, p.addr.city);

    return 0;
}
