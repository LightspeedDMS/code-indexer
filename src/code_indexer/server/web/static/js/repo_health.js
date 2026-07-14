/**
 * Repository Health Status Card Module
 *
 * Provides UI functionality for displaying and managing repository health status:
 * - Fetching health data from REST endpoint
 * - Displaying health status indicators (healthy/unhealthy)
 * - Expanding/collapsing detailed health check results
 * - Refresh button with force_refresh support
 * - Loading spinner during data fetch
 * - State tracking for health details across HTMX refreshes
 *
 * Story #60: Enhanced Repo Details Card
 * Bug Fix: Health details auto-close on HTMX refresh
 */

// Track open health details panels to survive HTMX content replacements
let openHealthDetailsSet = new Set();

/**
 * Submit an async health-check background job (Bug #1394).
 *
 * Shared by all four health-check call sites (golden repo details,
 * global-activated badge, global-activated details, activated repo
 * details) -- endpointBase selects which router the job is submitted
 * through.
 *
 * @param {string} endpointBase - '/api/repositories' or '/api/activated-repos'
 * @param {string} repoAlias - Repository alias (golden repo alias or user alias)
 * @param {boolean} forceRefresh - Whether to bypass cache
 * @returns {Promise<object>} Parsed JSON body containing at least job_id
 */
async function submitHealthCheckJob(endpointBase, repoAlias, forceRefresh = false) {
    const csrfToken = document.querySelector('input[name="csrf_token"]')?.value || '';
    const url = `${endpointBase}/${encodeURIComponent(repoAlias)}/health/check${forceRefresh ? '?force_refresh=true' : ''}`;

    const response = await fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': csrfToken
        },
        credentials: 'same-origin',
        body: JSON.stringify({})
    });

    if (!response.ok) {
        // HTTP-status message, with the parsed server `detail` (when present)
        // taking precedence -- matches the POST-submitter convention already
        // used elsewhere in this codebase (e.g. activated_repo_management.js's
        // add-index/reindex submitters).
        let detail = null;
        try {
            const errorData = await response.json();
            detail = errorData.detail;
        } catch (parseError) {
            // No JSON body to parse -- fall back to the HTTP-status message below.
        }
        const submitError = new Error(detail || `HTTP ${response.status}: ${response.statusText}`);
        // Expose the raw status code so callers can branch on it reliably
        // (e.g. 404 = "not indexed yet") without string-matching the message,
        // which no longer always contains the status code once `detail` wins.
        submitError.status = response.status;
        throw submitError;
    }

    try {
        return await response.json();
    } catch (error) {
        throw new Error(`Failed to parse response JSON: ${error.message}`);
    }
}

/**
 * Poll a health-check background job until it reaches a terminal state.
 *
 * Callback-based (not hardcoded to any one DOM id scheme) so it can serve
 * all four health-check call sites, each with different container ids.
 *
 * @param {string} jobId - Job ID to poll
 * @param {object} callbacks - { onUpdate(jobStatus), onComplete(jobStatus), onError(error) }
 */
function pollHealthCheckJob(jobId, { onUpdate, onComplete, onError } = {}) {
    const pollIntervalMs = 5000; // 5 seconds
    const maxPolls = 360; // 30 minutes max
    let pollCount = 0;

    const poll = async () => {
        pollCount++;

        if (pollCount > maxPolls) {
            if (onError) {
                onError(new Error('Job polling timeout. Please refresh the page.'));
            }
            return;
        }

        try {
            const response = await fetch(`/api/jobs/${jobId}`, {
                credentials: 'same-origin'
            });

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }

            const jobStatus = await response.json();

            if (onUpdate) {
                onUpdate(jobStatus);
            }

            if (jobStatus.status === 'pending' || jobStatus.status === 'running') {
                setTimeout(poll, pollIntervalMs);
            } else if (jobStatus.status === 'completed') {
                if (onComplete) {
                    onComplete(jobStatus);
                }
            } else {
                // failed / cancelled
                if (onError) {
                    onError(new Error(jobStatus.error || `Job ${jobStatus.status}`));
                }
            }
        } catch (error) {
            if (onError) {
                onError(error);
            }
        }
    };

    poll();
}

/**
 * Render health status indicator.
 *
 * @param {object} healthData - Repository health result with collections
 * @returns {string} HTML for health status indicator (Acceptance Criteria 1 & 2)
 */
function renderHealthIndicator(healthData) {
    if (!healthData) {
        return '<span class="health-status health-unknown">Unknown</span>';
    }

    // Handle new multi-collection response format
    if (healthData.overall_healthy !== undefined) {
        const { overall_healthy, healthy_count, total_collections } = healthData;

        if (overall_healthy) {
            // All collections healthy
            const title = total_collections > 0
                ? `All ${total_collections} collection(s) healthy`
                : 'No collections indexed';

            return `
                <span class="health-status health-healthy" title="${title}">
                    <span class="health-icon">✓</span> Healthy (${healthy_count}/${total_collections})
                </span>
            `;
        } else {
            // Some collections unhealthy
            const title = `${healthData.unhealthy_count} of ${total_collections} collection(s) unhealthy`;

            return `
                <span class="health-status health-unhealthy" title="${title}">
                    <span class="health-icon">✗</span> Issues (${healthy_count}/${total_collections})
                </span>
            `;
        }
    }

    // Legacy single-collection format (backward compatibility)
    if (healthData.valid) {
        return `
            <span class="health-status health-healthy" title="Index health check passed">
                <span class="health-icon">✓</span> Healthy
            </span>
        `;
    } else {
        const errorCount = healthData.errors ? healthData.errors.length : 0;
        const title = errorCount > 0
            ? `Index health check failed: ${errorCount} error(s)`
            : 'Index health check failed';

        return `
            <span class="health-status health-unhealthy" title="${title}">
                <span class="health-icon">✗</span> Unhealthy
            </span>
        `;
    }
}

/**
 * Render detailed health check results.
 *
 * @param {object} healthData - Repository health result with collections
 * @returns {string} HTML for detailed view (Acceptance Criteria 4 & 5)
 */
function renderHealthDetails(healthData) {
    if (!healthData) {
        return '<p class="health-error">No health data available</p>';
    }

    // Handle new multi-collection response format
    if (healthData.collections !== undefined) {
        const { collections, total_collections, healthy_count, unhealthy_count } = healthData;

        if (total_collections === 0) {
            return '<p class="health-info">No HNSW indexes found in this repository.</p>';
        }

        let html = '';

        // Summary at top
        html += '<div class="health-summary">';
        html += `<strong>Collections:</strong> ${total_collections} total, `;
        html += `<span class="health-pass">${healthy_count} healthy</span>`;
        if (unhealthy_count > 0) {
            html += `, <span class="health-fail">${unhealthy_count} unhealthy</span>`;
        }
        html += '</div>';

        // Per-collection details
        collections.forEach(collection => {
            const statusClass = collection.valid ? 'collection-healthy' : 'collection-unhealthy';
            const statusIcon = collection.valid ? '✓' : '✗';

            html += `<div class="collection-health ${statusClass}">`;
            html += `<h5><span class="collection-icon">${statusIcon}</span> ${escapeHtml(collection.collection_name)} <span class="collection-type">(${collection.index_type})</span></h5>`;

            // Checks grid
            const checks = [
                { label: 'File Exists', value: collection.file_exists },
                { label: 'Readable', value: collection.readable },
                { label: 'Loadable', value: collection.loadable },
                { label: 'Has Data', value: collection.element_count !== null && collection.element_count > 0 },
            ];

            if (collection.loadable) {
                checks.push({ label: 'Integrity Valid', value: collection.valid });
            }

            html += '<div class="health-checks-grid">';
            checks.forEach(check => {
                const icon = check.value ? '✓' : '✗';
                const checkClass = check.value ? 'check-pass' : 'check-fail';
                html += `
                    <div class="health-check-item ${checkClass}">
                        <span class="check-icon">${icon}</span>
                        <span class="check-label">${check.label}</span>
                    </div>
                `;
            });
            html += '</div>';

            // Errors
            if (collection.errors && collection.errors.length > 0) {
                html += '<div class="health-errors">';
                html += '<strong>Errors:</strong><ul class="health-error-list">';
                collection.errors.forEach(error => {
                    html += `<li class="health-error-item">${escapeHtml(error)}</li>`;
                });
                html += '</ul></div>';
            }

            // Metadata
            if (collection.element_count !== null && collection.element_count !== undefined) {
                html += '<div class="health-metadata">';
                html += `<div class="metadata-item"><span class="metadata-label">Vectors:</span> <span class="metadata-value">${collection.element_count.toLocaleString()}</span></div>`;
                if (collection.connections_checked) {
                    html += `<div class="metadata-item"><span class="metadata-label">Connections Checked:</span> <span class="metadata-value">${collection.connections_checked.toLocaleString()}</span></div>`;
                }
                if (collection.min_inbound !== null) {
                    html += `<div class="metadata-item"><span class="metadata-label">Min Inbound:</span> <span class="metadata-value">${collection.min_inbound}</span></div>`;
                }
                if (collection.max_inbound !== null) {
                    html += `<div class="metadata-item"><span class="metadata-label">Max Inbound:</span> <span class="metadata-value">${collection.max_inbound}</span></div>`;
                }
                if (collection.orphan_count !== null && collection.orphan_count !== undefined) {
                    html += `<div class="metadata-item"><span class="metadata-label">Orphan Count:</span> <span class="metadata-value">${Number(collection.orphan_count)}</span></div>`;
                }
                html += '</div>';
            }

            html += '</div>'; // close collection-health
        });

        return html;
    }

    // Legacy single-collection format (backward compatibility)
    const checks = [
        { label: 'File Exists', value: healthData.file_exists },
        { label: 'Readable', value: healthData.readable },
        { label: 'Loadable', value: healthData.loadable },
        { label: 'Has Data', value: healthData.element_count !== null && healthData.element_count > 0 },
    ];

    if (healthData.loadable) {
        checks.push({ label: 'Integrity Valid', value: healthData.valid });
    }

    let html = '<div class="health-checks-grid">';
    checks.forEach(check => {
        const icon = check.value ? '✓' : '✗';
        const statusClass = check.value ? 'check-pass' : 'check-fail';
        html += `
            <div class="health-check-item ${statusClass}">
                <span class="check-icon">${icon}</span>
                <span class="check-label">${check.label}</span>
            </div>
        `;
    });
    html += '</div>';

    if (healthData.errors && healthData.errors.length > 0) {
        html += '<div class="health-errors"><h5>Error Details:</h5><ul class="health-error-list">';
        healthData.errors.forEach(error => {
            html += `<li class="health-error-item">${escapeHtml(error)}</li>`;
        });
        html += '</ul></div>';
    }

    if (healthData.element_count !== null && healthData.element_count !== undefined) {
        html += `<div class="health-metadata">
            <div class="metadata-item"><span class="metadata-label">Vectors:</span> <span class="metadata-value">${healthData.element_count.toLocaleString()}</span></div>
            ${healthData.connections_checked ? `<div class="metadata-item"><span class="metadata-label">Connections Checked:</span> <span class="metadata-value">${healthData.connections_checked.toLocaleString()}</span></div>` : ''}
            ${healthData.min_inbound !== null ? `<div class="metadata-item"><span class="metadata-label">Min Inbound:</span> <span class="metadata-value">${healthData.min_inbound}</span></div>` : ''}
            ${healthData.max_inbound !== null ? `<div class="metadata-item"><span class="metadata-label">Max Inbound:</span> <span class="metadata-value">${healthData.max_inbound}</span></div>` : ''}
            ${(healthData.orphan_count !== null && healthData.orphan_count !== undefined) ? `<div class="metadata-item"><span class="metadata-label">Orphan Count:</span> <span class="metadata-value">${Number(healthData.orphan_count)}</span></div>` : ''}
        </div>`;
    }

    return html;
}

/**
 * Toggle health details expansion (Acceptance Criteria 3).
 * Tracks state in openHealthDetailsSet to survive HTMX refreshes.
 *
 * @param {string} repoAlias - Repository alias
 */
async function toggleHealthDetails(repoAlias) {
    const detailsContainer = document.getElementById(`health-details-${repoAlias}`);
    const indicator = document.getElementById(`health-indicator-${repoAlias}`);

    if (!detailsContainer) {
        console.error(`Health details container not found for: ${repoAlias}`);
        return;
    }

    // Toggle visibility
    const isExpanded = detailsContainer.style.display !== 'none';

    if (isExpanded) {
        // Collapse
        detailsContainer.style.display = 'none';
        if (indicator) {
            indicator.classList.remove('expanded');
        }
        // Remove from state tracking
        openHealthDetailsSet.delete(repoAlias);
    } else {
        // Expand
        detailsContainer.style.display = 'block';
        if (indicator) {
            indicator.classList.add('expanded');
        }
        // Add to state tracking
        openHealthDetailsSet.add(repoAlias);

        // Load health data if not already loaded
        if (detailsContainer.dataset.loaded !== 'true') {
            await loadHealthDetails(repoAlias, false);
        }
    }
}

/**
 * Load and display health details.
 *
 * @param {string} repoAlias - Repository alias
 * @param {boolean} forceRefresh - Whether to bypass cache (Acceptance Criteria 7)
 */
async function loadHealthDetails(repoAlias, forceRefresh = false) {
    const detailsContainer = document.getElementById(`health-details-${repoAlias}`);
    const refreshBtn = document.getElementById(`health-refresh-${repoAlias}`);

    if (!detailsContainer) {
        return;
    }

    // Bug #1394: golden_repo_details.html already has a job-progress-{alias}
    // container used by other operations (reindex, add-index) on this page --
    // reuse it for the health-check job's spinner/status while polling, in
    // addition to the existing health-loading placeholder below.
    const jobProgressContainer = document.getElementById(`job-progress-${repoAlias}`);
    const jobStatusText = document.getElementById(`job-status-text-${repoAlias}`);
    const jobSpinner = document.getElementById(`job-spinner-${repoAlias}`);
    const jobProgressDetails = document.getElementById(`job-progress-details-${repoAlias}`);

    const showJobProgress = () => {
        if (jobProgressContainer && jobStatusText && jobSpinner) {
            jobProgressContainer.style.display = 'block';
            jobStatusText.textContent = 'checking health...';
            jobStatusText.style.color = 'var(--pico-color-blue-550)';
            jobSpinner.style.display = 'inline-block';
            if (jobProgressDetails) {
                jobProgressDetails.innerHTML = '';
            }
        }
    };

    const hideJobProgress = () => {
        if (jobProgressContainer) {
            jobProgressContainer.style.display = 'none';
        }
        if (jobSpinner) {
            jobSpinner.style.display = 'none';
        }
    };

    const handleError = (error) => {
        console.error(`Failed to load health data for ${repoAlias}:`, error);
        detailsContainer.innerHTML = `
            <p class="health-error">Failed to load health data: ${escapeHtml(error.message)}</p>
            <button class="outline small" onclick="loadHealthDetails('${escapeHtml(repoAlias)}', false)">Retry</button>
        `;
        hideJobProgress();
        if (refreshBtn) {
            refreshBtn.disabled = false;
        }
    };

    // Show loading spinner (Acceptance Criteria 8)
    detailsContainer.innerHTML = `
        <div class="health-loading">
            <span class="spinner"></span> Loading health data...
        </div>
    `;

    // Disable refresh button during load
    if (refreshBtn) {
        refreshBtn.disabled = true;
    }

    showJobProgress();

    try {
        const submitResult = await submitHealthCheckJob('/api/repositories', repoAlias, forceRefresh);

        pollHealthCheckJob(submitResult.job_id, {
            onComplete: (jobStatus) => {
                const healthData = jobStatus.result;

                // Render details
                detailsContainer.innerHTML = renderHealthDetails(healthData);
                detailsContainer.dataset.loaded = 'true';

                // Update indicator if present
                const indicatorContainer = document.getElementById(`health-indicator-${repoAlias}`);
                if (indicatorContainer) {
                    indicatorContainer.innerHTML = renderHealthIndicator(healthData);
                }

                hideJobProgress();
                if (refreshBtn) {
                    refreshBtn.disabled = false;
                }
            },
            onError: handleError
        });
    } catch (error) {
        handleError(error);
    }
}

/**
 * Refresh health data with force_refresh=true (Acceptance Criteria 7).
 *
 * @param {string} repoAlias - Repository alias
 */
async function refreshHealthData(repoAlias) {
    // Reset loaded flag to force reload
    const detailsContainer = document.getElementById(`health-details-${repoAlias}`);
    if (detailsContainer) {
        detailsContainer.dataset.loaded = 'false';
    }

    // Load with force_refresh=true
    await loadHealthDetails(repoAlias, true);
}

/**
 * Restore open health details panels after HTMX content replacement.
 * Called from htmx:afterSettle handler to maintain state across refreshes.
 *
 * Bug Fix: After HTMX replaces DOM, the new elements are empty.
 * We must both show the container AND reload the health data.
 */
function restoreOpenHealthDetails() {
    console.log('[CIDX] restoreOpenHealthDetails() called, openHealthDetailsSet size:', openHealthDetailsSet.size);

    openHealthDetailsSet.forEach(repoAlias => {
        const detailsContainer = document.getElementById(`health-details-${repoAlias}`);
        const indicator = document.getElementById(`health-indicator-${repoAlias}`);

        console.log('[CIDX] Restoring health details for:', repoAlias, 'container found:', !!detailsContainer);

        if (detailsContainer) {
            // Show the container
            detailsContainer.style.display = 'block';
            if (indicator) {
                indicator.classList.add('expanded');
            }

            // CRITICAL: Reload the health data since HTMX replaced the DOM
            // The new container is empty after DOM replacement
            detailsContainer.dataset.loaded = 'false';
            loadHealthDetails(repoAlias, false).then(() => {
                console.log('[CIDX] Health details reloaded for:', repoAlias);
            }).catch(err => {
                console.error('[CIDX] Failed to reload health details for:', repoAlias, err);
            });
        }
    });
}

/**
 * Escape HTML to prevent XSS.
 *
 * @param {string} text - Text to escape
 * @returns {string} Escaped HTML
 */
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Export functions for global use
window.toggleHealthDetails = toggleHealthDetails;
window.loadHealthDetails = loadHealthDetails;
window.refreshHealthData = refreshHealthData;
window.restoreOpenHealthDetails = restoreOpenHealthDetails;
window.renderHealthIndicator = renderHealthIndicator;
window.renderHealthDetails = renderHealthDetails;
window.escapeHtml = escapeHtml;
window.submitHealthCheckJob = submitHealthCheckJob;
window.pollHealthCheckJob = pollHealthCheckJob;
