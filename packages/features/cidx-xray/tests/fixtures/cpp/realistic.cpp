/* realistic.cpp — classes, namespace, template, try/catch, std:: usage */

#include <iostream>
#include <string>
#include <vector>
#include <stdexcept>
#include <algorithm>

namespace indexer {

// A simple key-value record
template <typename K, typename V>
struct Record {
    K key;
    V value;
    bool valid = true;

    Record(K k, V v) : key(std::move(k)), value(std::move(v)) {}
};

// Generic in-memory cache backed by a vector
template <typename K, typename V>
class Cache {
public:
    explicit Cache(std::size_t capacity) : capacity_(capacity) {}

    // Insert or update a record; throws std::overflow_error when full
    void put(const K &key, const V &value) {
        for (auto &r : records_) {
            if (r.valid && r.key == key) {
                r.value = value;
                return;
            }
        }
        if (records_.size() >= capacity_) {
            throw std::overflow_error("cache is full");
        }
        records_.emplace_back(key, value);
    }

    // Retrieve a value by key; throws std::out_of_range if absent
    const V &get(const K &key) const {
        for (const auto &r : records_) {
            if (r.valid && r.key == key) {
                return r.value;
            }
        }
        throw std::out_of_range("key not found: " + to_string(key));
    }

    // Remove a key; returns true if found and removed
    bool remove(const K &key) {
        for (auto &r : records_) {
            if (r.valid && r.key == key) {
                r.valid = false;
                return true;
            }
        }
        return false;
    }

    std::size_t size() const {
        std::size_t n = 0;
        for (const auto &r : records_) {
            if (r.valid) ++n;
        }
        return n;
    }

    void dump() const {
        std::cout << "Cache[" << size() << "/" << capacity_ << "]:\n";
        for (const auto &r : records_) {
            if (r.valid) {
                std::cout << "  " << r.key << " => " << r.value << "\n";
            }
        }
    }

private:
    std::size_t capacity_;
    std::vector<Record<K, V>> records_;

    // Helper: convert key to string for error messages
    static std::string to_string(const K &k) {
        return std::to_string(k);
    }
    static std::string to_string(const std::string &k) { return k; }
};

// Word frequency counter built on Cache<string, int>
class WordCounter {
public:
    explicit WordCounter(std::size_t max_words = 1024)
        : cache_(max_words) {}

    void add(const std::string &word) {
        try {
            int current = cache_.get(word);
            cache_.put(word, current + 1);
        } catch (const std::out_of_range &) {
            cache_.put(word, 1);
        }
    }

    int count(const std::string &word) const {
        try {
            return cache_.get(word);
        } catch (const std::out_of_range &) {
            return 0;
        }
    }

    void report() const { cache_.dump(); }

private:
    Cache<std::string, int> cache_;
};

} // namespace indexer

int main() {
    using namespace indexer;

    WordCounter wc;
    std::vector<std::string> words = {
        "hello", "world", "hello", "foo", "world", "world"
    };

    for (const auto &w : words) {
        wc.add(w);
    }

    wc.report();
    std::cout << "hello appears " << wc.count("hello") << " times\n";

    Cache<int, std::string> ic(4);
    ic.put(1, "one");
    ic.put(2, "two");
    ic.put(3, "three");

    try {
        std::cout << ic.get(99) << "\n";
    } catch (const std::out_of_range &e) {
        std::cerr << "caught: " << e.what() << "\n";
    }

    return 0;
}
