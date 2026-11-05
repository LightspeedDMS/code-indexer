/* realistic.c — a representative C module for indexing and AST tests */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Maximum number of records the cache can hold */
#define MAX_RECORDS 256
#define KEY_LEN 64

typedef struct {
    char key[KEY_LEN];
    int  value;
    int  valid;
} Record;

typedef struct {
    Record entries[MAX_RECORDS];
    int    count;
} Cache;

/* Initialize a cache to its empty state */
static void cache_init(Cache *c) {
    memset(c, 0, sizeof(*c));
}

/* Insert or update a key/value pair; returns 0 on success, -1 on full */
int cache_put(Cache *c, const char *key, int value) {
    if (c->count >= MAX_RECORDS) {
        return -1;
    }
    /* Check for existing key first */
    for (int i = 0; i < c->count; i++) {
        if (c->entries[i].valid && strcmp(c->entries[i].key, key) == 0) {
            c->entries[i].value = value;
            return 0;
        }
    }
    /* New entry */
    Record *r = &c->entries[c->count++];
    strncpy(r->key, key, KEY_LEN - 1);
    r->key[KEY_LEN - 1] = '\0';
    r->value = value;
    r->valid = 1;
    return 0;
}

/* Lookup a key; returns pointer to value or NULL if not found */
int *cache_get(Cache *c, const char *key) {
    for (int i = 0; i < c->count; i++) {
        if (c->entries[i].valid && strcmp(c->entries[i].key, key) == 0) {
            return &c->entries[i].value;
        }
    }
    return NULL;
}

/* Remove a key; returns 1 if removed, 0 if not found */
int cache_remove(Cache *c, const char *key) {
    for (int i = 0; i < c->count; i++) {
        if (c->entries[i].valid && strcmp(c->entries[i].key, key) == 0) {
            c->entries[i].valid = 0;
            return 1;
        }
    }
    return 0;
}

/* Print all valid cache entries */
void cache_dump(const Cache *c) {
    printf("Cache dump (%d entries):\n", c->count);
    for (int i = 0; i < c->count; i++) {
        if (c->entries[i].valid) {
            printf("  [%d] key=\"%s\" value=%d\n",
                   i, c->entries[i].key, c->entries[i].value);
        }
    }
}

/* Compute sum of all valid values */
long cache_sum(const Cache *c) {
    long sum = 0;
    int  i   = 0;
    while (i < c->count) {
        if (c->entries[i].valid) {
            sum += c->entries[i].value;
        }
        i++;
    }
    return sum;
}

int main(void) {
    Cache cache;
    cache_init(&cache);

    cache_put(&cache, "alpha", 10);
    cache_put(&cache, "beta",  20);
    cache_put(&cache, "gamma", 30);

    int *v = cache_get(&cache, "beta");
    if (v != NULL) {
        printf("beta = %d\n", *v);
    }

    cache_remove(&cache, "beta");
    cache_dump(&cache);
    printf("sum = %ld\n", cache_sum(&cache));

    return 0;
}
