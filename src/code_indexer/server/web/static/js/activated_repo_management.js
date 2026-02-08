/**
 * Activated Repository Management
 *
 * Provides UI functionality for managing activated repositories:
 * - Fetching and displaying index status (semantic, FTS, temporal, SCIP)
 * - Triggering reindex operations
 * - Adding new index types
 * - Checking repository health
 * - Syncing with golden repository source
 * - Switching branches
 * - Listing available branches
 *
 * Story #160: Activated Repository Management Feature Parity
 */

/**
 * Fetch index status for an activated repository
 * @param {string} userAlias - User alias of the activated repository
 * @param {string} owner - Repository owner username (for admin checking other users' repos)
 * @returns {Promise<object>} Index status with has_semantic, has_fts, has_temporal, has_scip
 */
async function fetchActivatedRepoIndexes(userAlias, owner = null) {
    let url = `/api/activated-repos/${encodeURIComponent(userAlias)}/indexes`;

    if (owner) {
        url += `?owner=${encodeURIComponent(owner)}`;
    }

    const response = await fetch(url, {
        credentials: 'same-origin'
    });

    if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    try {
        return await response.json();
    } catch (error) {
        throw new Error(`Failed to parse response JSON: ${error.message}`);
    }
}

/**
 * Update index status badges in the UI
 * @param {string} userAlias - User alias
 * @param {object} indexData - Index status data (API response with indexes array)
 */
function updateIndexBadges(userAlias, indexData) {
    const badgesContainer = document.getElementById(`index-badges-${userAlias}`);
    if (!badgesContainer) {
        return;
    }

    // Convert API response format (indexes array) to flat boolean format
    // API returns: {indexes: [{index_type: "semantic", exists: true}, ...]}
    // We need: {has_semantic: true, has_fts: false, ...}
    let has_semantic = false;
    let has_fts = false;
    let has_temporal = false;
    let has_scip = false;

    if (indexData.indexes && Array.isArray(indexData.indexes)) {
        indexData.indexes.forEach(idx => {
            if (idx.exists) {
                if (idx.index_type === 'semantic') has_semantic = true;
                else if (idx.index_type === 'fts') has_fts = true;
                else if (idx.index_type === 'temporal') has_temporal = true;
                else if (idx.index_type === 'scip') has_scip = true;
            }
        });
    } else {
        // Fallback for legacy flat format (if API returns has_* directly)
        has_semantic = indexData.has_semantic || false;
        has_fts = indexData.has_fts || false;
        has_temporal = indexData.has_temporal || false;
        has_scip = indexData.has_scip || false;
    }

    let html = '';
    if (has_semantic) {
        html += '<span class="index-badge semantic" title="Semantic search available">Semantic</span>';
    }
    if (has_fts) {
        html += '<span class="index-badge fts" title="Full-text search available">FTS</span>';
    }
    if (has_temporal) {
        html += '<span class="index-badge temporal" title="Git history search available">Temporal</span>';
    }
    if (has_scip) {
        html += '<span class="index-badge scip" title="SCIP code intelligence available">SCIP</span>';
    }
    if (!has_semantic && !has_fts && !has_temporal && !has_scip) {
        html = '<span class="no-indexes">None</span>';
    }

    badgesContainer.innerHTML = html;
}

/**
 * Show the add index form for an activated repository
 * @param {string} userAlias - User alias
 */
function showActivatedRepoAddIndexForm(userAlias) {
    const form = document.getElementById(`add-index-form-${userAlias}`);
    if (form) {
        form.style.display = 'block';
        // Track open form state for HTMX refresh preservation
        if (typeof openActivatedRepoAddIndexForms !== 'undefined') {
            openActivatedRepoAddIndexForms.add(userAlias);
            console.log('[CIDX] Tracking open add-index form for activated repo:', userAlias);
        }
    }
}

/**
 * Hide the add index form for an activated repository
 * @param {string} userAlias - User alias
 */
function hideActivatedRepoAddIndexForm(userAlias) {
    const form = document.getElementById(`add-index-form-${userAlias}`);
    if (form) {
        form.style.display = 'none';
        // Remove from tracking when form is closed
        if (typeof openActivatedRepoAddIndexForms !== 'undefined') {
            openActivatedRepoAddIndexForms.delete(userAlias);
            console.log('[CIDX] Stopped tracking add-index form for activated repo:', userAlias);
        }
    }
}

/**
 * Submit add index request for an activated repository
 * @param {string} userAlias - User alias
 */
async function submitActivatedRepoAddIndex(userAlias) {
    // Collect selected index type from dropdown
    const select = document.getElementById(`add-index-type-${userAlias}`);
    if (!select) {
        alert('Index type selector not found');
        return;
    }

    const indexType = select.value;
    if (!indexType) {
        alert('Please select an index type');
        return;
    }

    // Get index type label for confirmation
    const selectedOption = select.options[select.selectedIndex];
    const typeLabel = selectedOption.text;

    // Confirmation dialog before submission
    if (!confirm(`Add ${typeLabel} index to repository "${userAlias}"?\n\nThis operation may take several minutes.`)) {
        return;
    }

    // Hide the form
    hideActivatedRepoAddIndexForm(userAlias);

    // Show job progress container with loading indicator
    const progressContainer = document.getElementById(`job-progress-${userAlias}`);
    const statusText = document.getElementById(`job-status-text-${userAlias}`);
    const spinner = document.getElementById(`job-spinner-${userAlias}`);

    if (progressContainer && statusText && spinner) {
        progressContainer.style.display = 'block';
        statusText.textContent = 'submitting...';
        spinner.style.display = 'inline-block';
    }

    try {
        // Get CSRF token from page
        const csrfToken = document.querySelector('input[name="csrf_token"]')?.value || '';

        // Submit POST request to add index
        const response = await fetch(`/api/activated-repos/${encodeURIComponent(userAlias)}/indexes/${indexType}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': csrfToken
            },
            credentials: 'same-origin',
            body: JSON.stringify({})
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Unknown error' }));
            throw new Error(errorData.detail || `HTTP ${response.status}: ${response.statusText}`);
        }

        const data = await response.json();
        const jobId = data.job_id;

        // Start polling job status
        if (jobId) {
            pollActivatedRepoJobStatus(userAlias, jobId, 'add-index');
        } else {
            throw new Error('No job_id returned from server');
        }

    } catch (error) {
        console.error('Failed to submit add index request:', error);

        // Show error feedback
        if (statusText) {
            statusText.textContent = 'error';
            statusText.style.color = 'var(--pico-color-red-550)';
        }
        if (spinner) {
            spinner.style.display = 'none';
        }

        const progressDetails = document.getElementById(`job-progress-details-${userAlias}`);
        if (progressDetails) {
            progressDetails.innerHTML = `<p class="error-text">Failed to submit job: ${escapeHtml(error.message)}</p>`;
        }

        // Show error message and allow retry
        setTimeout(() => {
            alert(`Failed to add index: ${error.message}\n\nPlease try again or contact administrator.`);
            if (progressContainer) {
                progressContainer.style.display = 'none';
            }
        }, 500);
    }
}

/**
 * Trigger reindex operation for an activated repository
 * @param {string} userAlias - User alias
 */
async function triggerActivatedRepoReindex(userAlias) {
    // Confirmation dialog
    if (!confirm(`Re-index repository "${userAlias}"?\n\nThis will rebuild all existing indexes and may take several minutes.`)) {
        return;
    }

    // Show job progress container with loading indicator
    const progressContainer = document.getElementById(`job-progress-${userAlias}`);
    const statusText = document.getElementById(`job-status-text-${userAlias}`);
    const spinner = document.getElementById(`job-spinner-${userAlias}`);

    if (progressContainer && statusText && spinner) {
        progressContainer.style.display = 'block';
        statusText.textContent = 'submitting...';
        spinner.style.display = 'inline-block';
    }

    try {
        // Get CSRF token from page
        const csrfToken = document.querySelector('input[name="csrf_token"]')?.value || '';

        // Submit POST request to trigger reindex
        const response = await fetch(`/api/activated-repos/${encodeURIComponent(userAlias)}/reindex`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': csrfToken
            },
            credentials: 'same-origin',
            body: JSON.stringify({})
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Unknown error' }));
            throw new Error(errorData.detail || `HTTP ${response.status}: ${response.statusText}`);
        }

        const data = await response.json();
        const jobId = data.job_id;

        // Start polling job status
        if (jobId) {
            pollActivatedRepoJobStatus(userAlias, jobId, 'reindex');
        } else {
            throw new Error('No job_id returned from server');
        }

    } catch (error) {
        console.error('Failed to submit reindex request:', error);

        // Show error feedback
        if (statusText) {
            statusText.textContent = 'error';
            statusText.style.color = 'var(--pico-color-red-550)';
        }
        if (spinner) {
            spinner.style.display = 'none';
        }

        const progressDetails = document.getElementById(`job-progress-details-${userAlias}`);
        if (progressDetails) {
            progressDetails.innerHTML = `<p class="error-text">Failed to submit job: ${escapeHtml(error.message)}</p>`;
        }

        // Show error message
        setTimeout(() => {
            alert(`Failed to trigger reindex: ${error.message}\n\nPlease try again or contact administrator.`);
            if (progressContainer) {
                progressContainer.style.display = 'none';
            }
        }, 500);
    }
}

/**
 * Fetch health data for an activated repository
 * @param {string} userAlias - User alias
 * @param {boolean} forceRefresh - Whether to bypass cache
 * @param {string} owner - Repository owner username (for admin checking other users' repos)
 * @returns {Promise<object>} Health check result
 */
async function fetchActivatedRepoHealth(userAlias, forceRefresh = false, owner = null) {
    let url = `/api/activated-repos/${encodeURIComponent(userAlias)}/health`;
    const params = new URLSearchParams();

    if (forceRefresh) {
        params.append('force_refresh', 'true');
    }

    if (owner) {
        params.append('owner', owner);
    }

    if (params.toString()) {
        url += '?' + params.toString();
    }

    const response = await fetch(url, {
        credentials: 'same-origin'
    });

    if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    try {
        return await response.json();
    } catch (error) {
        throw new Error(`Failed to parse response JSON: ${error.message}`);
    }
}

/**
 * Toggle health details expansion for activated repository
 * @param {string} userAlias - User alias
 * @param {string} owner - Repository owner username (for admin checking other users' repos)
 */
async function toggleActivatedRepoHealthDetails(userAlias, owner = null) {
    const detailsContainer = document.getElementById(`health-details-${userAlias}`);
    const indicator = document.getElementById(`health-indicator-${userAlias}`);

    if (!detailsContainer) {
        console.error(`Health details container not found for: ${userAlias}`);
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
        if (typeof openActivatedRepoHealthDetails !== 'undefined') {
            openActivatedRepoHealthDetails.delete(userAlias);
        }
    } else {
        // Expand
        detailsContainer.style.display = 'block';
        if (indicator) {
            indicator.classList.add('expanded');
        }
        // Add to state tracking
        if (typeof openActivatedRepoHealthDetails !== 'undefined') {
            openActivatedRepoHealthDetails.add(userAlias);
        }

        // Load health data if not already loaded
        if (detailsContainer.dataset.loaded !== 'true') {
            await loadActivatedRepoHealthDetails(userAlias, false, owner);
        }
    }
}

/**
 * Load and display health details for activated repository
 * @param {string} userAlias - User alias
 * @param {boolean} forceRefresh - Whether to bypass cache
 * @param {string} owner - Repository owner username (for admin checking other users' repos)
 */
async function loadActivatedRepoHealthDetails(userAlias, forceRefresh = false, owner = null) {
    const detailsContainer = document.getElementById(`health-details-${userAlias}`);
    const refreshBtn = document.getElementById(`health-refresh-${userAlias}`);

    if (!detailsContainer) {
        return;
    }

    try {
        // Show loading spinner
        detailsContainer.innerHTML = `
            <div class="health-loading">
                <span class="spinner"></span> Loading health data...
            </div>
        `;

        // Disable refresh button during load
        if (refreshBtn) {
            refreshBtn.disabled = true;
        }

        // Fetch health data
        const healthData = await fetchActivatedRepoHealth(userAlias, forceRefresh, owner);

        // Render details (reuse renderHealthDetails from repo_health.js)
        if (typeof renderHealthDetails === 'function') {
            detailsContainer.innerHTML = renderHealthDetails(healthData);
        } else {
            detailsContainer.innerHTML = '<p class="health-info">Health data loaded successfully.</p>';
        }
        detailsContainer.dataset.loaded = 'true';

        // Update indicator if present
        const indicatorContainer = document.getElementById(`health-indicator-${userAlias}`);
        if (indicatorContainer && typeof renderHealthIndicator === 'function') {
            indicatorContainer.innerHTML = renderHealthIndicator(healthData);
        }

    } catch (error) {
        console.error(`Failed to load health data for ${userAlias}:`, error);
        detailsContainer.innerHTML = `
            <p class="health-error">Failed to load health data: ${escapeHtml(error.message)}</p>
            <button class="outline small" onclick="loadActivatedRepoHealthDetails('${escapeHtml(userAlias)}', false, ${owner ? `'${escapeHtml(owner)}'` : 'null'})">Retry</button>
        `;
    } finally {
        // Re-enable refresh button
        if (refreshBtn) {
            refreshBtn.disabled = false;
        }
    }
}

/**
 * Refresh health data with force_refresh=true for activated repository
 * @param {string} userAlias - User alias
 * @param {string} owner - Repository owner username (for admin checking other users' repos)
 */
async function refreshActivatedRepoHealthData(userAlias, owner = null) {
    // Reset loaded flag to force reload
    const detailsContainer = document.getElementById(`health-details-${userAlias}`);
    if (detailsContainer) {
        detailsContainer.dataset.loaded = 'false';
    }

    // Load with force_refresh=true
    await loadActivatedRepoHealthDetails(userAlias, true, owner);
}

/**
 * Trigger sync operation with golden repository
 * @param {string} userAlias - User alias
 */
async function syncWithGoldenRepo(userAlias) {
    // Confirmation dialog
    if (!confirm(`Sync repository "${userAlias}" with its golden source?\n\nThis will pull latest changes from the golden repository.`)) {
        return;
    }

    // Show job progress container with loading indicator
    const progressContainer = document.getElementById(`job-progress-${userAlias}`);
    const statusText = document.getElementById(`job-status-text-${userAlias}`);
    const spinner = document.getElementById(`job-spinner-${userAlias}`);

    if (progressContainer && statusText && spinner) {
        progressContainer.style.display = 'block';
        statusText.textContent = 'syncing...';
        spinner.style.display = 'inline-block';
    }

    try {
        // Get CSRF token from page
        const csrfToken = document.querySelector('input[name="csrf_token"]')?.value || '';

        // Submit POST request to sync
        const response = await fetch(`/api/activated-repos/${encodeURIComponent(userAlias)}/sync`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': csrfToken
            },
            credentials: 'same-origin',
            body: JSON.stringify({ reindex: true })
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Unknown error' }));
            throw new Error(errorData.detail || `HTTP ${response.status}: ${response.statusText}`);
        }

        const data = await response.json();

        // Show success feedback
        if (statusText) {
            statusText.textContent = 'completed';
            statusText.style.color = 'var(--pico-color-green-550)';
        }
        if (spinner) {
            spinner.style.display = 'none';
        }

        const progressDetails = document.getElementById(`job-progress-details-${userAlias}`);
        if (progressDetails) {
            progressDetails.innerHTML = `<p class="success-text">Sync completed successfully!</p>`;
        }

        // Hide progress after delay and refresh list
        setTimeout(() => {
            if (progressContainer) {
                progressContainer.style.display = 'none';
            }
            showActivatedRepoSuccessMessage('Repository synced successfully!');
            // Trigger HTMX refresh
            const refreshBtn = document.getElementById('refresh-btn');
            if (refreshBtn) {
                refreshBtn.click();
            }
        }, 2000);

    } catch (error) {
        console.error('Failed to sync repository:', error);

        // Show error feedback
        if (statusText) {
            statusText.textContent = 'error';
            statusText.style.color = 'var(--pico-color-red-550)';
        }
        if (spinner) {
            spinner.style.display = 'none';
        }

        const progressDetails = document.getElementById(`job-progress-details-${userAlias}`);
        if (progressDetails) {
            progressDetails.innerHTML = `<p class="error-text">Failed to sync: ${escapeHtml(error.message)}</p>`;
        }

        // Show error message
        setTimeout(() => {
            alert(`Failed to sync repository: ${error.message}\n\nPlease try again or contact administrator.`);
            if (progressContainer) {
                progressContainer.style.display = 'none';
            }
        }, 500);
    }
}

/**
 * Fetch available branches for an activated repository
 * @param {string} userAlias - User alias
 * @returns {Promise<object>} Branches data
 */
async function fetchActivatedRepoBranches(userAlias) {
    const url = `/api/activated-repos/${encodeURIComponent(userAlias)}/branches`;

    const response = await fetch(url, {
        credentials: 'same-origin'
    });

    if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    try {
        return await response.json();
    } catch (error) {
        throw new Error(`Failed to parse response JSON: ${error.message}`);
    }
}

/**
 * Show branch selector modal
 * @param {string} userAlias - User alias
 */
async function showBranchSelector(userAlias) {
    const modal = document.getElementById('branch-selector-modal');
    const branchList = document.getElementById('branch-list');
    const loadingIndicator = document.getElementById('branch-loading');
    const errorContainer = document.getElementById('branch-error');

    if (!modal || !branchList) {
        alert('Branch selector modal not found');
        return;
    }

    // Store current user alias
    modal.dataset.userAlias = userAlias;

    // Show modal
    modal.showModal();

    // Show loading state
    branchList.style.display = 'none';
    errorContainer.style.display = 'none';
    loadingIndicator.style.display = 'block';

    try {
        // Fetch branches
        const data = await fetchActivatedRepoBranches(userAlias);

        // Populate branch list
        branchList.innerHTML = '';
        data.branches.forEach(branch => {
            const li = document.createElement('li');
            const button = document.createElement('button');
            button.className = branch.is_current ? 'secondary' : 'outline';
            button.textContent = branch.name + (branch.is_current ? ' (current)' : '');
            button.disabled = branch.is_current;
            button.onclick = () => switchBranch(userAlias, branch.name);
            li.appendChild(button);
            branchList.appendChild(li);
        });

        // Show branch list
        loadingIndicator.style.display = 'none';
        branchList.style.display = 'block';

    } catch (error) {
        console.error('Failed to fetch branches:', error);

        // Show error
        loadingIndicator.style.display = 'none';
        errorContainer.innerHTML = `<p class="error-text">Failed to load branches: ${escapeHtml(error.message)}</p>`;
        errorContainer.style.display = 'block';
    }
}

/**
 * Close branch selector modal
 */
function closeBranchSelector() {
    const modal = document.getElementById('branch-selector-modal');
    if (modal) {
        modal.close();
    }
}

/**
 * Switch to a different branch
 * @param {string} userAlias - User alias
 * @param {string} branchName - Branch name to switch to
 */
async function switchBranch(userAlias, branchName) {
    // Close modal
    closeBranchSelector();

    // Confirmation dialog
    if (!confirm(`Switch repository "${userAlias}" to branch "${branchName}"?\n\nThis will change the active branch and re-index the repository.`)) {
        return;
    }

    // Show job progress container with loading indicator
    const progressContainer = document.getElementById(`job-progress-${userAlias}`);
    const statusText = document.getElementById(`job-status-text-${userAlias}`);
    const spinner = document.getElementById(`job-spinner-${userAlias}`);

    if (progressContainer && statusText && spinner) {
        progressContainer.style.display = 'block';
        statusText.textContent = 'switching branch...';
        spinner.style.display = 'inline-block';
    }

    try {
        // Get CSRF token from page
        const csrfToken = document.querySelector('input[name="csrf_token"]')?.value || '';

        // Submit POST request to switch branch
        const response = await fetch(`/api/activated-repos/${encodeURIComponent(userAlias)}/branch`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': csrfToken
            },
            credentials: 'same-origin',
            body: JSON.stringify({ branch_name: branchName })
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Unknown error' }));
            throw new Error(errorData.detail || `HTTP ${response.status}: ${response.statusText}`);
        }

        const data = await response.json();
        const jobId = data.job_id;

        // Start polling job status
        if (jobId) {
            pollActivatedRepoJobStatus(userAlias, jobId, 'switch-branch');
        } else {
            // No job ID, immediate success
            if (statusText) {
                statusText.textContent = 'completed';
                statusText.style.color = 'var(--pico-color-green-550)';
            }
            if (spinner) {
                spinner.style.display = 'none';
            }

            setTimeout(() => {
                if (progressContainer) {
                    progressContainer.style.display = 'none';
                }
                showActivatedRepoSuccessMessage(`Switched to branch "${branchName}" successfully!`);
                // Trigger HTMX refresh
                const refreshBtn = document.getElementById('refresh-btn');
                if (refreshBtn) {
                    refreshBtn.click();
                }
            }, 1000);
        }

    } catch (error) {
        console.error('Failed to switch branch:', error);

        // Show error feedback
        if (statusText) {
            statusText.textContent = 'error';
            statusText.style.color = 'var(--pico-color-red-550)';
        }
        if (spinner) {
            spinner.style.display = 'none';
        }

        const progressDetails = document.getElementById(`job-progress-details-${userAlias}`);
        if (progressDetails) {
            progressDetails.innerHTML = `<p class="error-text">Failed to switch branch: ${escapeHtml(error.message)}</p>`;
        }

        // Show error message
        setTimeout(() => {
            alert(`Failed to switch branch: ${error.message}\n\nPlease try again or contact administrator.`);
            if (progressContainer) {
                progressContainer.style.display = 'none';
            }
        }, 500);
    }
}

/**
 * Poll job status every 5 seconds until completion
 * @param {string} userAlias - User alias
 * @param {string} jobId - Job ID to poll
 * @param {string} operationType - Type of operation (add-index, reindex, switch-branch)
 */
function pollActivatedRepoJobStatus(userAlias, jobId, operationType) {
    const progressContainer = document.getElementById(`job-progress-${userAlias}`);
    const statusText = document.getElementById(`job-status-text-${userAlias}`);
    const spinner = document.getElementById(`job-spinner-${userAlias}`);
    const progressDetails = document.getElementById(`job-progress-details-${userAlias}`);

    if (!progressContainer || !statusText || !spinner || !progressDetails) {
        console.error(`Progress elements not found for alias: ${userAlias}`);
        return;
    }

    const pollInterval = 5000; // 5 seconds
    let pollCount = 0;
    const maxPolls = 360; // 30 minutes max

    const poll = async () => {
        pollCount++;

        if (pollCount > maxPolls) {
            console.error(`Max polling attempts reached for job ${jobId}`);
            statusText.textContent = 'timeout';
            statusText.style.color = 'var(--pico-color-red-550)';
            spinner.style.display = 'none';
            progressDetails.innerHTML = '<p class="error-text">Job polling timeout. Please refresh the page.</p>';
            return;
        }

        try {
            const response = await fetch(`/api/jobs/${jobId}`);

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }

            const jobStatus = await response.json();
            updateActivatedRepoJobProgress(userAlias, jobStatus);

            // Continue polling if job is still running
            if (jobStatus.status === 'pending' || jobStatus.status === 'running') {
                setTimeout(poll, pollInterval);
            } else if (jobStatus.status === 'completed') {
                // Success feedback - reload page to show updates
                setTimeout(() => {
                    const successMessages = {
                        'add-index': 'Index added successfully!',
                        'reindex': 'Repository re-indexed successfully!',
                        'switch-branch': 'Branch switched successfully!'
                    };
                    showActivatedRepoSuccessMessage(successMessages[operationType] || 'Operation completed successfully!');
                    // Trigger HTMX refresh
                    const refreshBtn = document.getElementById('refresh-btn');
                    if (refreshBtn) {
                        refreshBtn.click();
                    }
                }, 1000);
            } else if (jobStatus.status === 'failed') {
                // Error feedback
                setTimeout(() => {
                    alert(`Operation failed: ${jobStatus.result?.error || 'Unknown error'}\n\nPlease try again or contact administrator.`);
                }, 500);
            }

        } catch (error) {
            console.error('Failed to poll job status:', error);
            statusText.textContent = 'error';
            statusText.style.color = 'var(--pico-color-red-550)';
            spinner.style.display = 'none';
            progressDetails.innerHTML = `<p class="error-text">Failed to retrieve job status: ${escapeHtml(error.message)}</p>`;
        }
    };

    // Start polling immediately
    poll();
}

/**
 * Update job progress UI with current status
 * @param {string} userAlias - User alias
 * @param {object} jobStatus - Job status object from API
 */
function updateActivatedRepoJobProgress(userAlias, jobStatus) {
    const statusText = document.getElementById(`job-status-text-${userAlias}`);
    const spinner = document.getElementById(`job-spinner-${userAlias}`);
    const progressDetails = document.getElementById(`job-progress-details-${userAlias}`);

    if (!statusText || !spinner || !progressDetails) {
        return;
    }

    // Update status text with color coding
    statusText.textContent = jobStatus.status;

    if (jobStatus.status === 'completed') {
        statusText.style.color = 'var(--pico-color-green-550)';
        spinner.style.display = 'none';
    } else if (jobStatus.status === 'failed') {
        statusText.style.color = 'var(--pico-color-red-550)';
        spinner.style.display = 'none';
    } else if (jobStatus.status === 'running') {
        statusText.style.color = 'var(--pico-color-blue-550)';
        spinner.style.display = 'inline-block';
    } else {
        // pending
        statusText.style.color = 'var(--pico-color-amber-550)';
        spinner.style.display = 'inline-block';
    }

    // Build progress details HTML
    let detailsHtml = '';

    if (jobStatus.created_at) {
        detailsHtml += `<p><strong>Created:</strong> ${new Date(jobStatus.created_at).toLocaleString()}</p>`;
    }
    if (jobStatus.started_at) {
        detailsHtml += `<p><strong>Started:</strong> ${new Date(jobStatus.started_at).toLocaleString()}</p>`;
    }
    if (jobStatus.completed_at) {
        detailsHtml += `<p><strong>Completed:</strong> ${new Date(jobStatus.completed_at).toLocaleString()}</p>`;
    }
    if (jobStatus.progress) {
        detailsHtml += `<p><strong>Progress:</strong> ${jobStatus.progress}</p>`;
    }
    if (jobStatus.result) {
        if (jobStatus.status === 'completed') {
            detailsHtml += `<p class="success-text"><strong>Result:</strong> ${JSON.stringify(jobStatus.result, null, 2)}</p>`;
        } else if (jobStatus.status === 'failed') {
            detailsHtml += `<p class="error-text"><strong>Error:</strong> ${jobStatus.result.error || JSON.stringify(jobStatus.result)}</p>`;
        }
    }

    progressDetails.innerHTML = detailsHtml;
}

/**
 * Show success message to user
 * @param {string} message - Success message to display
 */
function showActivatedRepoSuccessMessage(message) {
    const messagesContainer = document.querySelector('.repos-header');
    if (!messagesContainer) {
        return;
    }

    const successArticle = document.createElement('article');
    successArticle.className = 'message-success';
    successArticle.innerHTML = `<p>${message}</p>`;

    messagesContainer.insertAdjacentElement('afterend', successArticle);

    // Remove message after 5 seconds
    setTimeout(() => {
        successArticle.remove();
    }, 5000);
}

/**
 * Escape HTML to prevent XSS
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
window.fetchActivatedRepoIndexes = fetchActivatedRepoIndexes;
window.updateIndexBadges = updateIndexBadges;
window.showActivatedRepoAddIndexForm = showActivatedRepoAddIndexForm;
window.hideActivatedRepoAddIndexForm = hideActivatedRepoAddIndexForm;
window.submitActivatedRepoAddIndex = submitActivatedRepoAddIndex;
window.triggerActivatedRepoReindex = triggerActivatedRepoReindex;
window.fetchActivatedRepoHealth = fetchActivatedRepoHealth;
window.toggleActivatedRepoHealthDetails = toggleActivatedRepoHealthDetails;
window.loadActivatedRepoHealthDetails = loadActivatedRepoHealthDetails;
window.refreshActivatedRepoHealthData = refreshActivatedRepoHealthData;
window.syncWithGoldenRepo = syncWithGoldenRepo;
window.fetchActivatedRepoBranches = fetchActivatedRepoBranches;
window.showBranchSelector = showBranchSelector;
window.closeBranchSelector = closeBranchSelector;
window.switchBranch = switchBranch;
