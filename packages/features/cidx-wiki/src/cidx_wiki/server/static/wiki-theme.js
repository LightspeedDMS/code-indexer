// Apply saved theme immediately to prevent flash.
// Loaded synchronously from <head> in wiki_base.html.
(function() {
    var saved = localStorage.getItem('wiki_theme');
    if (saved) document.documentElement.setAttribute('data-theme', saved);
})();
