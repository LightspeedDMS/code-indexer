using System;
using System.Collections.Generic;
using System.Linq;

namespace Example.Pathological
{
    // Long single-line LINQ chain
    public static class LongLine
    {
        public static IReadOnlyList<string> Process(IEnumerable<string> items) =>
            items.Where(s => s != null).Select(s => s.Trim()).Where(s => s.Length >= 2).Select(s => s.ToLowerInvariant()).Select(s => string.Concat(s.Select((c, i) => i == 0 ? char.ToUpper(c) : c))).Distinct().OrderBy(s => s.Length).ThenBy(s => s).ToList();
    }

    // Deeply nested ternary
    public static class Classifier
    {
        public static string Classify(int x) =>
            x < 0
                ? x < -1000
                    ? x < -10000 ? "astronomically negative" : "deeply negative"
                    : x < -100 ? "moderately negative" : "slightly negative"
                : x == 0
                    ? "zero"
                    : x < 100
                        ? x < 10 ? "tiny" : "small"
                        : x < 10000
                            ? x < 1000 ? "medium" : "large"
                            : "huge";
    }

    // Many parameters
    public static class Builder
    {
        public static Dictionary<string, object> Build(
            string id, string name, string email, string phone,
            string address, string city, string state, string zip,
            string country, bool active, int priority, double score,
            IEnumerable<string> tags, Dictionary<string, string> meta) =>
            new()
            {
                ["id"] = id, ["name"] = name, ["email"] = email,
                ["phone"] = phone, ["address"] = address, ["city"] = city,
                ["state"] = state, ["zip"] = zip, ["country"] = country,
                ["active"] = active, ["priority"] = priority, ["score"] = score,
                ["tags"] = tags.ToList(), ["meta"] = meta,
            };
    }

    // Nested generic constraints
    public class SortedGroup<TKey, TValue>
        where TKey : IComparable<TKey>
        where TValue : IComparable<TValue>
    {
        private readonly SortedDictionary<TKey, SortedSet<TValue>> _data = new();

        public void Add(TKey key, TValue value)
        {
            if (!_data.TryGetValue(key, out var set))
            {
                set = new SortedSet<TValue>();
                _data[key] = set;
            }
            set.Add(value);
        }

        public IEnumerable<TValue> Get(TKey key) =>
            _data.TryGetValue(key, out var set) ? set : Enumerable.Empty<TValue>();

        public IOrderedEnumerable<IGrouping<TKey, TValue>> AllGrouped() =>
            _data.SelectMany(kvp => kvp.Value.Select(v => (kvp.Key, v)))
                 .GroupBy(x => x.Key, x => x.v)
                 .OrderBy(g => g.Key);
    }

    // Pattern matching with nested switch
    public static class MultiMatch
    {
        public static string Handle(object obj) => obj switch
        {
            int n when n > 0 => n switch
            {
                < 10 => "small int",
                < 100 => "medium int",
                _ => "large int"
            },
            int n when n < 0 => "negative int",
            string s when s.Length > 10 => $"long string ({s.Length})",
            string s => $"short string: {s}",
            IEnumerable<object> e => $"collection ({e.Count()} items)",
            null => "null",
            _ => $"unknown: {obj.GetType().Name}",
        };
    }
}
