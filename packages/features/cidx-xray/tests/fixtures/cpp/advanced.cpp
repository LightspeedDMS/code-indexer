/* advanced.cpp — templates, specialization, lambdas, operator overloading,
   inheritance, RAII */

#include <iostream>
#include <memory>
#include <vector>
#include <algorithm>
#include <functional>
#include <string>

// ---------------------------------------------------------------------------
// RAII resource guard
// ---------------------------------------------------------------------------
template <typename T, typename Deleter = std::default_delete<T>>
class ScopedResource {
public:
    explicit ScopedResource(T *ptr = nullptr) : ptr_(ptr) {}
    ~ScopedResource() { Deleter()(ptr_); }

    ScopedResource(const ScopedResource &) = delete;
    ScopedResource &operator=(const ScopedResource &) = delete;

    ScopedResource(ScopedResource &&other) noexcept : ptr_(other.ptr_) {
        other.ptr_ = nullptr;
    }

    T *get() const { return ptr_; }
    T &operator*() const { return *ptr_; }
    T *operator->() const { return ptr_; }
    explicit operator bool() const { return ptr_ != nullptr; }

private:
    T *ptr_;
};

// ---------------------------------------------------------------------------
// Simple 2D vector with operator overloading
// ---------------------------------------------------------------------------
struct Vec2 {
    double x, y;

    Vec2(double x = 0, double y = 0) : x(x), y(y) {}

    Vec2 operator+(const Vec2 &o) const { return {x + o.x, y + o.y}; }
    Vec2 operator-(const Vec2 &o) const { return {x - o.x, y - o.y}; }
    Vec2 operator*(double s)       const { return {x * s, y * s}; }
    bool operator==(const Vec2 &o) const { return x == o.x && y == o.y; }
    bool operator!=(const Vec2 &o) const { return !(*this == o); }

    double dot(const Vec2 &o) const { return x * o.x + y * o.y; }

    friend std::ostream &operator<<(std::ostream &os, const Vec2 &v) {
        return os << "(" << v.x << ", " << v.y << ")";
    }
};

// ---------------------------------------------------------------------------
// Template base + specialization
// ---------------------------------------------------------------------------
template <typename T>
T clamp(T val, T lo, T hi) {
    return val < lo ? lo : (val > hi ? hi : val);
}

// Full specialization for const char* (lexicographic clamp)
template <>
const char *clamp<const char *>(const char *val, const char *lo, const char *hi) {
    if (std::string(val) < std::string(lo)) return lo;
    if (std::string(val) > std::string(hi)) return hi;
    return val;
}

// ---------------------------------------------------------------------------
// Inheritance + virtual dispatch
// ---------------------------------------------------------------------------
class Shape {
public:
    virtual ~Shape() = default;
    virtual double area() const = 0;
    virtual std::string name() const = 0;

    void describe() const {
        std::cout << name() << ": area=" << area() << "\n";
    }
};

class Circle : public Shape {
public:
    explicit Circle(double r) : r_(r) {}
    double area() const override { return 3.14159265358979 * r_ * r_; }
    std::string name() const override { return "Circle"; }
private:
    double r_;
};

class Rectangle : public Shape {
public:
    Rectangle(double w, double h) : w_(w), h_(h) {}
    double area() const override { return w_ * h_; }
    std::string name() const override { return "Rectangle"; }
private:
    double w_, h_;
};

// ---------------------------------------------------------------------------
// Lambda-heavy pipeline
// ---------------------------------------------------------------------------
std::vector<double> transform_areas(const std::vector<std::unique_ptr<Shape>> &shapes,
                                    std::function<double(double)> transform) {
    std::vector<double> result;
    result.reserve(shapes.size());
    std::transform(shapes.begin(), shapes.end(), std::back_inserter(result),
                   [&](const std::unique_ptr<Shape> &s) {
                       return transform(s->area());
                   });
    return result;
}

int main() {
    // RAII
    ScopedResource<int> res(new int(42));
    std::cout << "resource = " << *res << "\n";

    // Operator overloading
    Vec2 a{1.0, 2.0}, b{3.0, 4.0};
    std::cout << "a + b = " << (a + b) << "\n";
    std::cout << "dot = " << a.dot(b) << "\n";

    // Template + specialization
    std::cout << "clamp(5,1,10) = " << clamp(5, 1, 10) << "\n";
    std::cout << "clamp(\"m\",\"a\",\"z\") = " << clamp("m", "a", "z") << "\n";

    // Inheritance + virtual dispatch
    std::vector<std::unique_ptr<Shape>> shapes;
    shapes.push_back(std::make_unique<Circle>(3.0));
    shapes.push_back(std::make_unique<Rectangle>(4.0, 5.0));
    for (const auto &s : shapes) { s->describe(); }

    // Lambda pipeline
    auto doubled = transform_areas(shapes, [](double a) { return a * 2.0; });
    for (double v : doubled) { std::cout << "doubled area: " << v << "\n"; }

    return 0;
}
