using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;

namespace Example.Services
{
    public enum UserRole { Admin, Editor, Viewer }

    public record User(
        long Id,
        string Name,
        string Email,
        UserRole Role,
        DateTime CreatedAt,
        bool Enabled = true
    )
    {
        public string DisplayName => $"{Name} <{Email}>";
        public bool IsAdmin => Role == UserRole.Admin;
    }

    public record CreateUserRequest(string Name, string Email, UserRole Role = UserRole.Viewer)
    {
        public IReadOnlyList<string> Validate()
        {
            var errors = new List<string>();
            if (string.IsNullOrWhiteSpace(Name)) errors.Add("name must not be blank");
            if (!Email.Contains('@')) errors.Add("email must contain @");
            if (Name.Length > 200) errors.Add("name must not exceed 200 characters");
            return errors;
        }
    }

    public class Page<T>
    {
        public IReadOnlyList<T> Items { get; init; } = Array.Empty<T>();
        public long Total { get; init; }
        public int PageIndex { get; init; }
        public int Size { get; init; }
        public int TotalPages => Size > 0 ? (int)Math.Ceiling((double)Total / Size) : 0;
        public bool HasNext => PageIndex < TotalPages - 1;
        public bool HasPrev => PageIndex > 0;

        public Page<TOut> Map<TOut>(Func<T, TOut> transform) =>
            new() { Items = Items.Select(transform).ToList(), Total = Total, PageIndex = PageIndex, Size = Size };
    }

    public class UserNotFoundException : Exception
    {
        public long UserId { get; }
        public UserNotFoundException(long userId)
            : base($"User {userId} not found") => UserId = userId;
    }

    public class EmailConflictException : Exception
    {
        public string Email { get; }
        public EmailConflictException(string email)
            : base($"Email already registered: {email}") => Email = email;
    }

    public interface IUserRepository
    {
        Task<User?> FindByIdAsync(long id);
        Task<(IReadOnlyList<User> Items, long Total)> FindPageAsync(int page, int size, UserRole? role = null);
        Task<User?> FindByEmailAsync(string email);
        Task<User> InsertAsync(User user);
        Task<User> UpdateAsync(User user);
        Task DeleteAsync(long id);
    }

    public class UserService
    {
        private const int DefaultPageSize = 20;
        private const int MaxPageSize = 100;
        private const int CacheTtlSeconds = 300;

        private readonly IUserRepository _repo;
        private readonly Dictionary<long, (User User, DateTime CachedAt)> _cache = new();

        public UserService(IUserRepository repo) => _repo = repo;

        public async Task<User> GetUserAsync(long id)
        {
            if (_cache.TryGetValue(id, out var entry) &&
                (DateTime.UtcNow - entry.CachedAt).TotalSeconds < CacheTtlSeconds)
            {
                return entry.User;
            }
            var user = await _repo.FindByIdAsync(id) ?? throw new UserNotFoundException(id);
            _cache[id] = (user, DateTime.UtcNow);
            return user;
        }

        public async Task<Page<User>> ListUsersAsync(
            int page = 0, int size = DefaultPageSize, UserRole? role = null)
        {
            var effectiveSize = Math.Clamp(size, 1, MaxPageSize);
            var (items, total) = await _repo.FindPageAsync(page, effectiveSize, role);
            return new Page<User> { Items = items, Total = total, PageIndex = page, Size = effectiveSize };
        }

        public async Task<User> CreateUserAsync(CreateUserRequest request)
        {
            var errors = request.Validate();
            if (errors.Count > 0)
                throw new ArgumentException($"Validation failed: {string.Join(", ", errors)}");

            var existing = await _repo.FindByEmailAsync(request.Email);
            if (existing is not null) throw new EmailConflictException(request.Email);

            var user = new User(0, request.Name.Trim(), request.Email.ToLowerInvariant().Trim(),
                request.Role, DateTime.UtcNow);
            return await _repo.InsertAsync(user);
        }

        public async Task DeleteUserAsync(long id)
        {
            _ = await GetUserAsync(id);
            await _repo.DeleteAsync(id);
            _cache.Remove(id);
        }

        public async Task<IReadOnlyList<User>> GetAdminsAsync() =>
            (await ListUsersAsync(size: MaxPageSize, role: UserRole.Admin))
            .Items
            .Where(u => u.Enabled)
            .OrderBy(u => u.Name)
            .ToList();

        public async Task<Dictionary<UserRole, int>> GetCountsByRoleAsync()
        {
            var allUsers = (await ListUsersAsync(size: MaxPageSize)).Items;
            return allUsers
                .GroupBy(u => u.Role)
                .ToDictionary(g => g.Key, g => g.Count());
        }
    }
}
