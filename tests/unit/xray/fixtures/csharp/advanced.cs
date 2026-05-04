using System;
using System.Collections.Generic;
using System.Linq;

namespace Example.Advanced
{
    // Records with positional and nominal syntax
    public record Point(double X, double Y)
    {
        public double DistanceTo(Point other) =>
            Math.Sqrt(Math.Pow(X - other.X, 2) + Math.Pow(Y - other.Y, 2));

        public static Point Origin => new(0, 0);
    }

    public record Circle(Point Center, double Radius) : IShape
    {
        public double Area => Math.PI * Radius * Radius;
        public double Perimeter => 2 * Math.PI * Radius;
    }

    public record Rectangle(Point TopLeft, double Width, double Height) : IShape
    {
        public double Area => Width * Height;
        public double Perimeter => 2 * (Width + Height);
        public bool IsSquare => Math.Abs(Width - Height) < double.Epsilon;
    }

    public interface IShape
    {
        double Area { get; }
        double Perimeter { get; }
    }

    // Pattern matching switch expression
    public static class ShapeAnalyzer
    {
        public static string Describe(IShape shape) => shape switch
        {
            Circle c when c.Radius > 100 => $"Large circle (r={c.Radius:F1})",
            Circle c => $"Circle (r={c.Radius:F1})",
            Rectangle { IsSquare: true } r => $"Square (side={r.Width:F1})",
            Rectangle r => $"Rectangle ({r.Width:F1}x{r.Height:F1})",
            null => throw new ArgumentNullException(nameof(shape)),
            _ => $"Unknown shape: {shape.GetType().Name}",
        };

        public static double TotalArea(IEnumerable<IShape> shapes) =>
            shapes.Sum(s => s.Area);

        public static IShape Largest(IEnumerable<IShape> shapes) =>
            shapes.MaxBy(s => s.Area)
            ?? throw new InvalidOperationException("No shapes provided");
    }

    // Generic result type
    public class Result<T>
    {
        private readonly T? _value;
        private readonly Exception? _error;

        private Result(T value) => _value = value;
        private Result(Exception error) => _error = error;

        public static Result<T> Ok(T value) => new(value);
        public static Result<T> Fail(Exception error) => new(error);

        public bool IsOk => _error is null;
        public T Value => IsOk ? _value! : throw new InvalidOperationException("Result is failure", _error);
        public Exception Error => !IsOk ? _error! : throw new InvalidOperationException("Result is success");

        public Result<TOut> Map<TOut>(Func<T, TOut> f) =>
            IsOk ? Result<TOut>.Ok(f(_value!)) : Result<TOut>.Fail(_error!);

        public T GetOrDefault(T defaultValue) => IsOk ? _value! : defaultValue;
    }

    // Fluent LINQ pipeline
    public static class DataPipeline
    {
        public static IEnumerable<(string Key, double Average, int Count)> Summarize(
            IEnumerable<(string Category, double Value)> data) =>
            data
                .GroupBy(x => x.Category)
                .Select(g => (
                    Key: g.Key,
                    Average: g.Average(x => x.Value),
                    Count: g.Count()
                ))
                .Where(s => s.Count > 1)
                .OrderByDescending(s => s.Average);
    }

    // Extension methods
    public static class Extensions
    {
        public static IEnumerable<T> WhereNotNull<T>(this IEnumerable<T?> source) where T : class =>
            source.Where(x => x is not null).Select(x => x!);

        public static TResult Pipe<T, TResult>(this T value, Func<T, TResult> fn) => fn(value);

        public static IEnumerable<T> Tap<T>(this IEnumerable<T> source, Action<T> action)
        {
            foreach (var item in source)
            {
                action(item);
                yield return item;
            }
        }
    }
}
