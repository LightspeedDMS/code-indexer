package com.example.pathological;

import java.util.*;
import java.util.stream.*;
import java.util.function.*;

/**
 * Pathological Java: deeply nested, long lines, comment-heavy.
 * This file is intentionally complex to stress-test the tree-sitter parser.
 * It is not intended to be idiomatic or readable.
 */
public class Pathological {

    // Very long single line with deeply nested method calls and lambdas
    public static String deepNest(List<List<List<String>>> data) { return data.stream().flatMap(outer -> outer.stream().flatMap(inner -> inner.stream().filter(s -> s != null && !s.isEmpty()).map(s -> s.trim().toLowerCase().replace(" ", "_").replaceAll("[^a-z0-9_]", "")).filter(s -> s.length() > 2))).sorted(Comparator.comparingInt(String::length).thenComparing(Comparator.naturalOrder())).distinct().collect(Collectors.joining(", ", "[", "]")); }

    // Deeply nested ternary expressions
    public static int classify(int x) {
        return x < 0 ? (x < -100 ? (x < -1000 ? -4 : (x < -500 ? -3 : -2)) : -1)
                     : (x == 0 ? 0
                               : (x < 100 ? (x < 10 ? 1 : (x < 50 ? 2 : 3))
                                           : (x < 1000 ? (x < 500 ? 4 : 5) : 6)));
    }

    // Method with many parameters (stress test for parameter list parsing)
    public static Map<String, Object> buildRecord(String id, String name, String email,
            String phone, String address, String city, String state, String zip,
            String country, boolean active, int priority, double score,
            List<String> tags, Map<String, String> metadata, Date createdAt) {
        Map<String, Object> record = new LinkedHashMap<>();
        record.put("id", id);
        record.put("name", name);
        record.put("email", email);
        record.put("phone", phone);
        record.put("address", address);
        record.put("city", city);
        record.put("state", state);
        record.put("zip", zip);
        record.put("country", country);
        record.put("active", active);
        record.put("priority", priority);
        record.put("score", score);
        record.put("tags", tags);
        record.put("metadata", metadata);
        record.put("createdAt", createdAt);
        return record;
    }

    // Interface with multiple method signatures (no annotations needed)
    public interface Operation {
        double apply(double x, double y);
        default String symbol() { return "?"; }
    }

    // Implementations via lambda-style anonymous classes without annotations
    public static final Operation PLUS = (x, y) -> x + y;
    public static final Operation MINUS = (x, y) -> x - y;
    public static final Operation TIMES = (x, y) -> x * y;
    public static final Operation DIVIDE = (x, y) -> {
        if (y == 0) throw new ArithmeticException("division by zero");
        return x / y;
    };

    // Anonymous class inside lambda inside stream
    public static List<Runnable> buildTasks(List<String> names) {
        return names.stream()
                .map(name -> (Runnable) () -> {
                    // Nested comment block
                    // doing work for: name
                    String processed = name
                            .trim()
                            .toLowerCase()
                            .replace(" ", "_");
                    System.out.println("Processing: " + processed);
                })
                .collect(Collectors.toList());
    }

    // Synchronized block with nested try-catch-finally
    private static final Object LOCK = new Object();
    private static int counter = 0;

    public static int incrementSafely() {
        synchronized (LOCK) {
            try {
                // comment: increment
                counter++;
                // comment: validate
                if (counter < 0) {
                    throw new ArithmeticException("overflow");
                }
                return counter;
            } catch (ArithmeticException e) {
                counter = Integer.MAX_VALUE;
                return counter;
            } finally {
                // comment: always runs
                System.out.println("counter=" + counter);
            }
        }
    }

    // Static initializer block
    private static final Map<String, Integer> PRIORITY_MAP;
    static {
        PRIORITY_MAP = new HashMap<>();
        PRIORITY_MAP.put("critical", 1);
        PRIORITY_MAP.put("high", 2);
        PRIORITY_MAP.put("medium", 3);
        PRIORITY_MAP.put("low", 4);
        PRIORITY_MAP.put("trivial", 5);
    }

    // Nested generic types stress test
    public static <K, V extends Comparable<V>> Map<K, List<V>> groupAndSort(
            Map<K, List<V>> input) {
        Map<K, List<V>> result = new LinkedHashMap<>();
        input.forEach((key, values) -> {
            List<V> sorted = values.stream()
                    .filter(Objects::nonNull)
                    .sorted()
                    .collect(Collectors.toList());
            result.put(key, sorted);
        });
        return result;
    }

    // Chained optional operations
    public static Optional<String> processName(Optional<String> input) {
        return input
                .filter(s -> s != null)
                .map(String::trim)
                .filter(s -> !s.isEmpty())
                .map(s -> s.substring(0, 1).toUpperCase() + s.substring(1).toLowerCase())
                .filter(s -> s.matches("[A-Za-z]+"));
    }
}
