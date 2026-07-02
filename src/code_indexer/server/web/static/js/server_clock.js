/**
 * Server Clock - Real-time server time display (Story #89)
 *
 * Displays current server time in admin navigation bar, synchronized with server.
 * Updates every second without page refresh, handles client-server time offset.
 */

(function() {
    'use strict';

    // Server time offset in milliseconds (calculated at initialization)
    let serverOffset = 0;

    /**
     * Format date as "YYYY-MM-DD HH:MM:SS UTC"
     * @param {Date} date - Date to format
     * @returns {string} Formatted date string
     */
    function formatServerTime(date) {
        const year = date.getUTCFullYear();
        const month = String(date.getUTCMonth() + 1).padStart(2, '0');
        const day = String(date.getUTCDate()).padStart(2, '0');
        const hours = String(date.getUTCHours()).padStart(2, '0');
        const minutes = String(date.getUTCMinutes()).padStart(2, '0');
        const seconds = String(date.getUTCSeconds()).padStart(2, '0');

        return `${year}-${month}-${day} ${hours}:${minutes}:${seconds} UTC`;
    }

    /**
     * Update the clock display with current server time
     */
    function updateClock() {
        const clockDisplay = document.querySelector('#server-clock .clock-display');
        if (!clockDisplay) return;

        // Get current time adjusted for server offset
        const now = new Date(Date.now() + serverOffset);
        clockDisplay.textContent = formatServerTime(now);
    }

    /**
     * Initialize server clock
     */
    function initServerClock() {
        const clockElement = document.getElementById('server-clock');
        if (!clockElement) {
            console.warn('Server clock element not found');
            return;
        }

        // Get initial server timestamp from data attribute
        const initialTimestamp = clockElement.getAttribute('data-initial-timestamp');
        if (!initialTimestamp) {
            console.error('Server clock: data-initial-timestamp attribute missing');
            return;
        }

        try {
            // Parse server time (ISO 8601 format)
            const serverTime = new Date(initialTimestamp);
            const clientTime = new Date();

            // Calculate offset between server and client
            // serverOffset = serverTime - clientTime
            serverOffset = serverTime.getTime() - clientTime.getTime();

            console.log(`Server clock initialized. Offset: ${serverOffset}ms`);

            // Update immediately
            updateClock();

            // Update every second
            setInterval(updateClock, 1000);

        } catch (error) {
            console.error('Server clock initialization failed:', error);
        }
    }

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initServerClock);
    } else {
        initServerClock();
    }

    // Re-initialize on HTMX page swaps (persists across navigation)
    document.addEventListener('htmx:afterSwap', function(event) {
        // Only re-initialize if clock element was replaced
        if (event.detail.target.querySelector('#server-clock')) {
            initServerClock();
        }
    });

})();
