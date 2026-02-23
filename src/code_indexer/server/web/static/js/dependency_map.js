/**
 * Dependency Map D3.js Visualization Module
 *
 * Interactive force-directed graph for domain dependency exploration.
 * Integrates with the two-panel domain explorer: graph node clicks and
 * domain list clicks both call selectDomain() — the single source of truth.
 *
 * Story #215: D3.js Interactive Dependency Graph Frontend
 * Requires D3.js v7 (loaded before this script in dependency_map.html).
 */

// ── Module state ──────────────────────────────────────────────────────────────

var _graphData = null;        // Raw JSON from server
var _simulation = null;       // D3 force simulation
var _svg = null;              // D3 SVG selection
var _graphGroup = null;       // Zoomable inner <g>
var _nodeSelection = null;    // D3 circle selection
var _edgeSelection = null;    // D3 line selection
var _labelSelection = null;   // D3 text selection
var _focusedDomain = null;    // Currently focused domain name (null = overview)
var _currentDepth = 2;        // BFS depth (1-3)
var _graphInitialized = false;
var _tooltip = null;          // Active tooltip element
var _zoom = null;             // D3 zoom behavior

// Constants
var BASE_RADIUS = 20, SCALE_FACTOR = 8, MIN_RADIUS = 20, MAX_RADIUS = 105;
var OPACITY_FULL = 1.0, OPACITY_DIM_NODE = 0.15;
var OPACITY_EDGE = 0.6, OPACITY_DIM_EDGE = 0.05;
// Node radius sizing factors (Story #260: bubble sizing by repo count + dep count)
var REPO_SCALE = 3, REPO_MAX_FACTOR = 30;
var DEP_SCALE = 4, DEP_MAX_FACTOR = 40;
var SYNERGY_SCALE = 0.5, SYNERGY_MAX = 15;

// ── Public API ────────────────────────────────────────────────────────────────

/**
 * Initialize the dependency graph. Idempotent — second call is a no-op.
 * Fetches graph JSON from dataUrl, then builds D3 force simulation.
 *
 * @param {string} containerId - Container div ID (parent of #dependency-graph-svg)
 * @param {string} dataUrl - URL returning {nodes:[...], edges:[...]}
 */
function initDependencyGraph(containerId, dataUrl) {
    if (_graphInitialized) return;
    var svgEl = document.getElementById('dependency-graph-svg');
    if (!svgEl || !document.getElementById(containerId)) return;
    _showLoadingState(svgEl);
    fetch(dataUrl, { credentials: 'same-origin' })
        .then(function(r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        })
        .then(function(data) {
            _graphData = data;
            _graphInitialized = true;
            _buildGraph(svgEl, data);
        })
        .catch(function(err) {
            console.error('[DepMap] fetch failed:', err);
            _showErrorState(svgEl, err.message);
        });
}

/**
 * Single source of truth for domain selection. Called by BOTH graph clicks
 * and domain list clicks. Updates graph highlight, list highlight, detail panel.
 *
 * @param {string} domainName - Domain name to select
 */
function selectDomain(domainName) {
    if (!domainName) return;
    _focusedDomain = domainName;
    if (_graphInitialized && _graphData) {
        updateGraphFocus(domainName, _currentDepth);
        _centerOnNode(domainName);
    }
    _highlightListItem(domainName);
    _loadDetailPanel(domainName);
}

/**
 * Set BFS depth level and update button active states.
 * Recalculates focus if a node is currently selected.
 *
 * @param {number} level - Depth 1, 2, or 3
 */
function setDepth(level) {
    _currentDepth = level;
    document.querySelectorAll('.depth-btn').forEach(function(btn) {
        var d = parseInt(btn.getAttribute('data-depth'), 10);
        if (d === level) {
            btn.setAttribute('aria-current', 'true');
            btn.classList.remove('outline');
        } else {
            btn.removeAttribute('aria-current');
            btn.classList.add('outline');
        }
    });
    if (_focusedDomain && _graphInitialized && _graphData) {
        updateGraphFocus(_focusedDomain, level);
    }
}

/**
 * BFS from startNodeId traversing both edge directions up to maxDepth.
 *
 * @param {object} graphData - {nodes:[...], edges:[...]}
 * @param {string} startNodeId
 * @param {number} maxDepth
 * @returns {Set<string>} Node IDs within depth
 */
function bfsTraversal(graphData, startNodeId, maxDepth) {
    var visited = new Set([startNodeId]);
    var queue = [{ id: startNodeId, depth: 0 }];
    var adj = {};
    graphData.nodes.forEach(function(n) { adj[n.id] = []; });
    graphData.edges.forEach(function(e) {
        var s = typeof e.source === 'object' ? e.source.id : e.source;
        var t = typeof e.target === 'object' ? e.target.id : e.target;
        if (adj[s]) adj[s].push(t);
        if (adj[t]) adj[t].push(s);
    });
    while (queue.length > 0) {
        var cur = queue.shift();
        if (cur.depth >= maxDepth) continue;
        (adj[cur.id] || []).forEach(function(nb) {
            if (!visited.has(nb)) {
                visited.add(nb);
                queue.push({ id: nb, depth: cur.depth + 1 });
            }
        });
    }
    return visited;
}

/**
 * Update node/edge opacity based on BFS depth from focused node.
 * focusedNodeId=null means overview mode (all full opacity).
 *
 * @param {string|null} focusedNodeId
 * @param {number} depth
 */
function updateGraphFocus(focusedNodeId, depth) {
    if (!_nodeSelection) return;
    if (!focusedNodeId) {
        _nodeSelection.style('opacity', OPACITY_FULL)
            .attr('stroke', _nodeStrokeColor).attr('stroke-width', 2);
        _edgeSelection.style('opacity', OPACITY_EDGE);
        _labelSelection.style('opacity', OPACITY_FULL);
        return;
    }
    var inDepth = bfsTraversal(_graphData, focusedNodeId, depth);
    _nodeSelection
        .style('opacity', function(d) { return inDepth.has(d.id) ? OPACITY_FULL : OPACITY_DIM_NODE; })
        .attr('stroke', function(d) { return d.id === focusedNodeId ? 'var(--pico-primary)' : _nodeStrokeColor(d); })
        .attr('stroke-width', function(d) { return d.id === focusedNodeId ? 3 : 2; });
    _edgeSelection.style('opacity', function(d) {
        var s = typeof d.source === 'object' ? d.source.id : d.source;
        var t = typeof d.target === 'object' ? d.target.id : d.target;
        return (inDepth.has(s) && inDepth.has(t)) ? OPACITY_EDGE : OPACITY_DIM_EDGE;
    });
    _labelSelection.style('opacity', function(d) { return inDepth.has(d.id) ? OPACITY_FULL : OPACITY_DIM_NODE; });
}

/**
 * Show HTML tooltip near cursor with domain name, description, repo count.
 *
 * @param {object} nodeData - {id, label, description, repo_count}
 * @param {MouseEvent} event
 */
function renderTooltip(nodeData, event) {
    destroyTooltip();
    var tip = document.createElement('div');
    tip.id = 'depmap-tooltip';
    tip.style.cssText = 'position:fixed;z-index:9999;background:var(--pico-card-background-color);' +
        'border:1px solid var(--pico-muted-border-color);border-radius:0.375rem;padding:0.5rem 0.75rem;' +
        'max-width:260px;pointer-events:none;font-size:0.85rem;color:var(--pico-color);' +
        'box-shadow:0 4px 12px rgba(0,0,0,0.3);';
    var desc = (nodeData.description || '').slice(0, 100);
    if (nodeData.description && nodeData.description.length > 100) desc += '...';
    tip.innerHTML = '<div style="font-weight:600;margin-bottom:0.25rem;">' + _esc(nodeData.label || nodeData.id) + '</div>' +
        (desc ? '<div style="color:var(--pico-muted-color);margin-bottom:0.3rem;">' + _esc(desc) + '</div>' : '') +
        '<div style="font-size:0.78rem;color:var(--pico-muted-color);">Repos: <strong>' + (nodeData.repo_count || 0) + '</strong></div>' +
        '<div style="font-size:0.78rem;color:var(--pico-muted-color);">Deps in: <strong>' + (nodeData.incoming_dep_count || 0) + '</strong>\u2003Deps out: <strong>' + (nodeData.outgoing_dep_count || 0) + '</strong></div>';
    document.body.appendChild(tip);
    _tooltip = tip;
    _positionTooltip(tip, event);
}

/**
 * Hide and remove the active tooltip.
 */
function destroyTooltip() {
    if (_tooltip) { _tooltip.parentNode && _tooltip.parentNode.removeChild(_tooltip); _tooltip = null; }
    var e = document.getElementById('depmap-tooltip');
    if (e) e.parentNode && e.parentNode.removeChild(e);
}

// ── Graph construction (split into focused helpers) ───────────────────────────

/**
 * Entry point for graph construction after data is loaded.
 *
 * @param {SVGElement} svgEl
 * @param {object} data - {nodes, edges}
 */
function _buildGraph(svgEl, data) {
    var loading = document.getElementById('depmap-loading-fo');
    if (loading) loading.parentNode.removeChild(loading);
    if (!data.nodes || data.nodes.length === 0) { _showEmptyState(svgEl); return; }

    var w = svgEl.clientWidth || svgEl.getBoundingClientRect().width || 800;
    var h = svgEl.clientHeight || svgEl.getBoundingClientRect().height || 350;
    var nodes = data.nodes.map(function(n) { return Object.assign({}, n); });
    var edges = data.edges.map(function(e) { return Object.assign({}, e); });

    _svg = _setupSvgViewbox(svgEl, w, h);
    _createArrowMarker(_svg);
    _graphGroup = _svg.append('g').attr('class', 'depmap-graph-group');
    _setupZoom(_svg, _graphGroup);
    _edgeSelection = _renderEdges(_graphGroup, edges);
    var nodeGroups = _renderNodeGroups(_graphGroup, nodes);
    _nodeSelection = nodeGroups.select('circle');
    _labelSelection = nodeGroups.select('text');
    _svg.on('click', function() { _focusedDomain = null; updateGraphFocus(null, _currentDepth); destroyTooltip(); });
    _simulation = _setupForceSimulation(nodes, edges, w, h);
}

/**
 * Configure SVG viewBox and return D3 selection.
 *
 * @param {SVGElement} svgEl
 * @param {number} w - width
 * @param {number} h - height
 * @returns {object} D3 selection
 */
function _setupSvgViewbox(svgEl, w, h) {
    return d3.select(svgEl)
        .attr('viewBox', '0 0 ' + w + ' ' + h)
        .attr('preserveAspectRatio', 'xMidYMid meet');
}

/**
 * Append arrowhead marker defs to svg.
 *
 * @param {object} svg - D3 SVG selection
 */
function _createArrowMarker(svg) {
    svg.append('defs').append('marker')
        .attr('id', 'depmap-arrow').attr('viewBox', '0 -5 10 10')
        .attr('refX', 10).attr('refY', 0).attr('markerWidth', 6)
        .attr('markerHeight', 6).attr('orient', 'auto')
        .append('path').attr('d', 'M0,-5L10,0L0,5')
        .attr('fill', 'var(--pico-muted-border-color)');
}

/**
 * Attach zoom/pan behavior to svg, transforming graphGroup.
 *
 * @param {object} svg - D3 SVG selection
 * @param {object} group - D3 group selection to transform
 */
function _setupZoom(svg, group) {
    _zoom = d3.zoom().scaleExtent([0.2, 4]).on('zoom', function(event) {
        group.attr('transform', event.transform);
    });
    svg.call(_zoom);
}

/**
 * Render edge lines into graphGroup.
 *
 * @param {object} group - D3 group selection
 * @param {Array} edges - Edge data
 * @returns {object} D3 line selection
 */
function _renderEdges(group, edges) {
    return group.append('g').attr('class', 'depmap-edges')
        .selectAll('line').data(edges).enter().append('line')
        .attr('stroke', 'var(--pico-muted-border-color)')
        .attr('stroke-width', 1.5)
        .attr('marker-end', 'url(#depmap-arrow)')
        .style('opacity', OPACITY_EDGE);
}

/**
 * Render node groups (circle + label) into graphGroup.
 * Attaches drag, click, and tooltip handlers.
 *
 * @param {object} group - D3 group selection
 * @param {Array} nodes - Node data
 * @returns {object} D3 node group selection
 */
function _renderNodeGroups(group, nodes) {
    var ng = group.append('g').attr('class', 'depmap-nodes')
        .selectAll('g').data(nodes).enter().append('g')
        .attr('class', 'depmap-node').style('cursor', 'pointer')
        .call(d3.drag().on('start', _dragStart).on('drag', _dragging).on('end', _dragEnd))
        .on('click', function(event, d) { event.stopPropagation(); selectDomain(d.id); })
        .on('mouseover', function(event, d) { renderTooltip(d, event); })
        .on('mousemove', function(event) { if (_tooltip) _positionTooltip(_tooltip, event); })
        .on('mouseout', destroyTooltip);
    ng.append('circle')
        .attr('r', function(d) { return _nodeRadius(d); })
        .attr('fill', 'var(--pico-color-azure-500, #3b82f6)')
        .attr('stroke', _nodeStrokeColor).attr('stroke-width', 2)
        .style('opacity', OPACITY_FULL)
        .style('transition', 'opacity 0.25s ease, stroke 0.15s ease');
    ng.append('text')
        .attr('text-anchor', 'middle')
        .attr('dy', function(d) { return _nodeRadius(d) + 14; })
        .attr('font-size', '11px').attr('fill', 'var(--pico-color)')
        .attr('pointer-events', 'none').attr('user-select', 'none')
        .style('opacity', OPACITY_FULL).style('transition', 'opacity 0.25s ease')
        .text(function(d) { return d.label || d.id; });
    return ng;
}

/**
 * Create and start D3 force simulation.
 *
 * @param {Array} nodes
 * @param {Array} edges
 * @param {number} w - width
 * @param {number} h - height
 * @returns {object} D3 simulation
 */
function _setupForceSimulation(nodes, edges, w, h) {
    var sim = d3.forceSimulation(nodes)
        .force('link', d3.forceLink(edges).id(function(d) { return d.id; }).distance(180))
        .force('charge', d3.forceManyBody().strength(-300))
        .force('center', d3.forceCenter(w / 2, h / 2))
        .force('collide', d3.forceCollide().radius(function(d) { return _nodeRadius(d) + 20; }))
        .alphaDecay(0.04)
        .on('tick', _onTick)
        .on('end', function() { sim.stop(); });
    return sim;
}

// ── Simulation tick ───────────────────────────────────────────────────────────

function _onTick() {
    if (_edgeSelection) {
        _edgeSelection
            .attr('x1', function(d) { return d.source.x; })
            .attr('y1', function(d) { return d.source.y; })
            .attr('x2', function(d) {
                var dx = d.target.x - d.source.x, dy = d.target.y - d.source.y;
                var dist = Math.sqrt(dx * dx + dy * dy) || 1;
                return d.target.x - (dx / dist) * _nodeRadius(d.target);
            })
            .attr('y2', function(d) {
                var dx = d.target.x - d.source.x, dy = d.target.y - d.source.y;
                var dist = Math.sqrt(dx * dx + dy * dy) || 1;
                return d.target.y - (dy / dist) * _nodeRadius(d.target);
            });
    }
    if (_graphGroup) {
        _graphGroup.selectAll('.depmap-node')
            .attr('transform', function(d) { return 'translate(' + d.x + ',' + d.y + ')'; });
    }
}

// ── Drag handlers ─────────────────────────────────────────────────────────────

function _dragStart(event, d) {
    if (!event.active) _simulation.alphaTarget(0.3).restart();
    d.fx = d.x; d.fy = d.y;
}
function _dragging(event, d) { d.fx = event.x; d.fy = event.y; }
function _dragEnd(event, d) {
    if (!event.active) _simulation.alphaTarget(0);
    d.fx = null;
    d.fy = null;
}

// ── Private helpers ───────────────────────────────────────────────────────────

function _nodeRadius(nodeOrCount) {
    var repoCount, totalDeps;
    if (typeof nodeOrCount === 'object' && nodeOrCount !== null) {
        repoCount = nodeOrCount.repo_count || 0;
        totalDeps = (nodeOrCount.incoming_dep_count || 0) + (nodeOrCount.outgoing_dep_count || 0);
    } else {
        repoCount = nodeOrCount || 0;
        totalDeps = 0;
    }
    var repoFactor = Math.min(repoCount * REPO_SCALE, REPO_MAX_FACTOR);
    var depFactor = Math.min(totalDeps * DEP_SCALE, DEP_MAX_FACTOR);
    var synergyBonus = Math.min(repoCount * totalDeps * SYNERGY_SCALE, SYNERGY_MAX);
    return Math.min(MIN_RADIUS + repoFactor + depFactor + synergyBonus, MAX_RADIUS);
}
function _nodeStrokeColor() { return 'var(--pico-muted-border-color)'; }
function _truncate(text, max) {
    if (!text) return '';
    return text.length <= max ? text : text.slice(0, max - 1) + '\u2026';
}
function _esc(text) {
    if (!text) return '';
    var d = document.createElement('div'); d.textContent = text; return d.innerHTML;
}

function _centerOnNode(nodeId) {
    if (!_svg || !_simulation || !_zoom) return;
    var svgEl = document.getElementById('dependency-graph-svg');
    if (!svgEl) return;
    var w = svgEl.clientWidth || 800, h = svgEl.clientHeight || 350;
    var nodes = _simulation.nodes();
    var t = null;
    for (var i = 0; i < nodes.length; i++) { if (nodes[i].id === nodeId) { t = nodes[i]; break; } }
    if (!t || t.x === undefined) return;
    var scale = 1.2;
    _svg.transition().duration(500).call(
        _zoom.transform,
        d3.zoomIdentity.translate(w / 2 - scale * t.x, h / 2 - scale * t.y).scale(scale)
    );
}

function _highlightListItem(domainName) {
    document.querySelectorAll('#domain-list .domain-item').forEach(function(item) {
        item.style.borderLeft = '3px solid transparent';
        var btn = item.querySelector('.domain-list-btn');
        if (btn) {
            btn.style.background = '';
            btn.style.color = '';
            var nameSpan = btn.querySelector('.domain-name');
            if (nameSpan) { nameSpan.style.fontWeight = ''; }
        }
    });
    var lower = domainName.toLowerCase();
    document.querySelectorAll('#domain-list .domain-item').forEach(function(item) {
        if ((item.getAttribute('data-name') || '') === lower) {
            item.style.borderLeft = '3px solid var(--pico-primary)';
            var btn = item.querySelector('.domain-list-btn');
            if (btn) {
                btn.style.background = 'rgba(59, 130, 246, 0.15)';
                btn.style.color = 'var(--pico-primary)';
                var nameSpan = btn.querySelector('.domain-name');
                if (nameSpan) { nameSpan.style.fontWeight = '700'; }
                btn.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
        }
    });
}

function _loadDetailPanel(domainName) {
    var url = '/admin/partials/depmap-domain-detail/' + encodeURIComponent(domainName);
    if (!document.getElementById('domain-detail-panel')) return;
    if (window.htmx) {
        htmx.ajax('GET', url, { target: '#domain-detail-panel', swap: 'innerHTML' });
    } else {
        console.warn('[DepMap] HTMX not loaded, cannot load detail panel');
    }
}

function _positionTooltip(tip, event) {
    var margin = 12, vw = window.innerWidth, vh = window.innerHeight;
    var left = event.clientX + margin, top = event.clientY + margin;
    tip.style.visibility = 'hidden'; tip.style.display = 'block';
    var tw = tip.offsetWidth || 200, th = tip.offsetHeight || 80;
    tip.style.visibility = '';
    if (left + tw > vw - margin) left = event.clientX - tw - margin;
    if (top + th > vh - margin) top = event.clientY - th - margin;
    tip.style.left = left + 'px'; tip.style.top = top + 'px';
}

function _showLoadingState(svgEl) {
    var fo = document.createElementNS('http://www.w3.org/2000/svg', 'foreignObject');
    fo.setAttribute('x', '10%'); fo.setAttribute('y', '40%');
    fo.setAttribute('width', '80%'); fo.setAttribute('height', '20%');
    fo.id = 'depmap-loading-fo';
    var div = document.createElement('div');
    div.setAttribute('xmlns', 'http://www.w3.org/1999/xhtml');
    div.style.cssText = 'text-align:center;color:var(--pico-muted-color);font-style:italic;font-size:0.9rem;';
    div.textContent = 'Loading graph data...';
    fo.appendChild(div); svgEl.appendChild(fo);
}

function _showErrorState(svgEl, message) {
    var loading = document.getElementById('depmap-loading-fo');
    if (loading) loading.parentNode.removeChild(loading);
    var fo = document.createElementNS('http://www.w3.org/2000/svg', 'foreignObject');
    fo.setAttribute('x', '5%'); fo.setAttribute('y', '35%');
    fo.setAttribute('width', '90%'); fo.setAttribute('height', '30%');
    var div = document.createElement('div');
    div.setAttribute('xmlns', 'http://www.w3.org/1999/xhtml');
    div.style.cssText = 'text-align:center;color:var(--pico-del-color,#ef4444);font-size:0.85rem;padding:0.5rem;';
    div.textContent = 'Failed to load graph: ' + message;
    fo.appendChild(div); svgEl.appendChild(fo);
}

function _showEmptyState(svgEl) {
    var fo = document.createElementNS('http://www.w3.org/2000/svg', 'foreignObject');
    fo.setAttribute('x', '10%'); fo.setAttribute('y', '35%');
    fo.setAttribute('width', '80%'); fo.setAttribute('height', '30%');
    var div = document.createElement('div');
    div.setAttribute('xmlns', 'http://www.w3.org/1999/xhtml');
    div.style.cssText = 'text-align:center;color:var(--pico-muted-color);font-style:italic;font-size:0.9rem;padding:0.5rem;';
    div.textContent = 'No domain dependency data available. Run a dependency analysis job to populate the graph.';
    fo.appendChild(div); svgEl.appendChild(fo);
}

// ── Exports ───────────────────────────────────────────────────────────────────

window.initDependencyGraph = initDependencyGraph;
window.selectDomain = selectDomain;
window.setDepth = setDepth;
window.bfsTraversal = bfsTraversal;
window.updateGraphFocus = updateGraphFocus;
window.renderTooltip = renderTooltip;
window.destroyTooltip = destroyTooltip;
