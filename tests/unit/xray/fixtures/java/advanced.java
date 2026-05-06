package com.example.advanced;

import java.util.List;
import java.util.Optional;
import java.util.function.Function;
import java.util.stream.Collectors;

/**
 * Advanced Java features: sealed classes, records, pattern matching switch.
 */
public class Advanced {

    // Sealed interface hierarchy
    public sealed interface Shape permits Circle, Rectangle, Triangle {}

    public record Circle(double radius) implements Shape {
        public double area() { return Math.PI * radius * radius; }
    }

    public record Rectangle(double width, double height) implements Shape {
        public double area() { return width * height; }
    }

    public record Triangle(double base, double height) implements Shape {
        public double area() { return 0.5 * base * height; }
    }

    // Pattern matching switch (Java 17+)
    public static String describeShape(Shape shape) {
        return switch (shape) {
            case Circle c -> "Circle with radius " + c.radius();
            case Rectangle r when r.width() == r.height() -> "Square with side " + r.width();
            case Rectangle r -> "Rectangle " + r.width() + "x" + r.height();
            case Triangle t -> "Triangle base=" + t.base() + " height=" + t.height();
        };
    }

    public static double computeArea(Shape shape) {
        return switch (shape) {
            case Circle c -> c.area();
            case Rectangle r -> r.area();
            case Triangle t -> t.area();
        };
    }

    // Generic bounded type
    public static <T extends Shape & Comparable<T>> T findLargest(List<T> shapes) {
        return shapes.stream()
                .reduce((a, b) -> a.compareTo(b) >= 0 ? a : b)
                .orElseThrow(() -> new IllegalArgumentException("Empty list"));
    }

    // Text blocks (Java 15+)
    public static final String JSON_TEMPLATE = """
            {
                "type": "%s",
                "area": %.2f,
                "perimeter": %.2f
            }
            """;

    // Nested records
    public record Point(double x, double y) {
        public double distanceTo(Point other) {
            double dx = this.x - other.x;
            double dy = this.y - other.y;
            return Math.sqrt(dx * dx + dy * dy);
        }
    }

    public record Line(Point start, Point end) {
        public double length() { return start.distanceTo(end); }
        public Point midpoint() {
            return new Point((start.x() + end.x()) / 2, (start.y() + end.y()) / 2);
        }
    }

    // Lambda with method references
    public static List<String> formatShapes(List<Shape> shapes) {
        return shapes.stream()
                .map(Advanced::describeShape)
                .sorted(String::compareTo)
                .collect(Collectors.toList());
    }

    // Optional chaining
    public static Optional<Double> safeArea(Shape shape) {
        return Optional.ofNullable(shape)
                .map(Advanced::computeArea)
                .filter(area -> area > 0);
    }

    // Nested lambda capturing outer scope
    public static Function<Double, Shape> circleFactory() {
        double defaultRadius = 1.0;
        return radius -> {
            double effective = radius > 0 ? radius : defaultRadius;
            return new Circle(effective);
        };
    }

    public static void main(String[] args) {
        List<Shape> shapes = List.of(
                new Circle(5.0),
                new Rectangle(3.0, 4.0),
                new Triangle(6.0, 8.0),
                new Rectangle(2.0, 2.0)
        );

        shapes.stream()
                .map(s -> String.format(JSON_TEMPLATE, s.getClass().getSimpleName(),
                        computeArea(s), 0.0))
                .forEach(System.out::println);

        formatShapes(shapes).forEach(System.out::println);
    }
}
