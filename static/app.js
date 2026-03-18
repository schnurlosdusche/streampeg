// Poll status every 5 seconds
function updateStatus() {
    fetch('/api/status', {credentials: 'include'})
    .then(r => r.json())
    .then(data => {
        // Update disk info
        if (data.disk) {
            const w = document.getElementById('disk-worker');
            const n = document.getElementById('disk-nas');
            if (w) w.textContent = data.disk.worker_free_gb + ' GB / ' + data.disk.worker_total_gb + ' GB';
            if (n) n.textContent = data.disk.nas_free_gb + ' GB / ' + data.disk.nas_total_gb + ' GB';
        }

        // Update stream rows on dashboard
        if (data.streams) {
            data.streams.forEach(s => {
                // Cache stream status for player bar
                _lastStreamStatus[s.id] = {
                    current_track: s.current_track,
                    cover_url: s.cover_url,
                    stream_name: s.name,
                    running: s.running,
                };

                const row = document.querySelector('tr[data-stream-id="' + s.id + '"]');
                if (!row) return;

                const statusCell = row.querySelector('.status-cell');
                if (statusCell) {
                    var rs = s.rec_state || '';
                    if (s.running && rs === 'skipping') {
                        statusCell.innerHTML = '<span class="status-skipping">' + t('dash.status_skipping') + '</span>';
                    } else if (s.running && rs === 'recording') {
                        statusCell.innerHTML = '<span class="status-recording">' + t('dash.status_recording') + '</span>';
                    } else if (s.running && rs === 'waiting') {
                        statusCell.innerHTML = '<span class="status-running">' + t('dash.status_running') + '</span>';
                    } else if (s.running) {
                        var ct = (s.current_track || '').trim();
                        var hasTrack = ct && ct !== 'recording' && ct !== '-' && ct.replace(/[\s-]/g, '') !== '';
                        if (hasTrack) {
                            statusCell.innerHTML = '<span class="status-recording">' + t('dash.status_recording') + '</span>';
                        } else {
                            statusCell.innerHTML = '<span class="status-running">' + t('dash.status_running') + '</span>';
                        }
                    } else {
                        statusCell.innerHTML = '<span class="status-stopped">' + t('dash.status_stopped') + '</span>';
                    }
                }

                // Stats subtitle under track info
                const track = row.querySelector('.track-cell');
                if (track) {
                    var trackHtml;
                    var rs = s.rec_state || '';
                    var ct = s.current_track || '';
                    var hasTrack = ct && ct !== 'recording' && ct !== '-' && ct.replace(/[\s-]/g, '') !== '';
                    if (rs === 'waiting') { trackHtml = t('dash.waiting'); }
                    else if (hasTrack) { trackHtml = ct; }
                    else if (s.running) { trackHtml = t('dash.waiting'); }
                    else { trackHtml = '-'; }
                    if (s.yt_stats) {
                        var sub;
                        if (s.record_mode === 'soundcloud') {
                            sub = s.yt_stats.dl_sc + ' SC';
                            if (s.dl_fallback) sub += ' + ' + s.yt_stats.dl_yt + ' YT';
                        } else {
                            sub = s.yt_stats.dl_yt + ' YT';
                            if (s.dl_fallback) sub += ' + ' + s.yt_stats.dl_sc + ' SC';
                        }
                        sub += ' / ' + s.yt_stats.songs_seen + ' ' + t('dash.heard');
                        if (s.running && s.bitrate) sub += ' · ' + s.bitrate + ' kbps';
                        trackHtml += '<small style="display:block;color:var(--pico-muted-color,#888);">' + sub + '</small>';
                    } else if (s.track_stats && (s.track_stats.recorded + s.track_stats.skipped) > 0) {
                        var recSub = s.track_stats.recorded + ' ' + t('dash.rec') + ' / ' + (s.track_stats.recorded + s.track_stats.skipped) + ' ' + t('dash.heard');
                        if (s.running && s.bitrate) recSub += ' · ' + s.bitrate + ' kbps';
                        trackHtml += '<small style="display:block;color:var(--pico-muted-color,#888);">' + recSub + '</small>';
                    }
                    var coverHtml = '';
                    if (s.cover_url) {
                        coverHtml = '<img src="' + s.cover_url + '" class="track-cover" alt="">';
                    }
                    track.innerHTML = coverHtml + '<span class="track-text">' + trackHtml + '</span>';
                }

                const files = row.querySelector('.files-cell');
                if (files) {
                    files.textContent = s.file_count;
                }

                // const recPct = row.querySelector('.rec-pct-cell');
                // if (recPct) {
                //     recPct.textContent = s.rec_pct !== undefined ? s.rec_pct : '-';
                // }

                // Update start/stop button
                const actions = row.querySelector('.actions-cell');
                if (actions) {
                    var startForm = actions.querySelector('.btn-start');
                    var stopForm = actions.querySelector('.btn-stop');
                    if (s.running && startForm) {
                        startForm.closest('form').outerHTML = '<form method="post" action="/stream/' + s.id + '/stop" class="inline"><button type="submit" class="btn-icon btn-stop outline" title="Stop">Stop</button></form>';
                    } else if (!s.running && stopForm) {
                        stopForm.closest('form').outerHTML = '<form method="post" action="/stream/' + s.id + '/start" class="inline"><button type="submit" class="btn-icon btn-start" title="Start">Start</button></form>';
                    }
                }
            });
        }
        // Refresh player bar with latest stream data
        _refreshPlayerBar();
    })
    .catch(() => {}); // Silently ignore errors
}

// Sync button handler
document.querySelectorAll('.sync-form').forEach(form => {
    form.addEventListener('submit', function(e) {
        e.preventDefault();
        const btn = form.querySelector('button');
        btn.textContent = '...';
        btn.disabled = true;
        fetch(form.action, {method: 'POST', credentials: 'include'})
            .then(r => r.json())
            .then(data => {
                btn.textContent = data.success ? t('general.ok') : t('general.error');
                setTimeout(() => {
                    btn.textContent = 'Sync';
                    btn.disabled = false;
                }, 2000);
            })
            .catch(() => {
                btn.textContent = 'Sync';
                btn.disabled = false;
            });
    });
});

// --- Browser listen (completely independent from cast player bar) ---
var _playerAudio = null;
var _playerStreamId = null;

function toggleListen(streamId, url) {
    if (!_playerAudio) {
        _playerAudio = new Audio();
        _playerAudio.addEventListener('error', function() { stopListen(); });
    }
    if (_playerStreamId === streamId) {
        stopListen();
        return;
    }
    _playerAudio.pause();
    _playerAudio.removeAttribute('src');
    _playerAudio.load();
    _playerStreamId = streamId;
    _playerAudio.src = url;
    _playerAudio.play();
    _updateListenIcons(streamId);
}

function stopListen() {
    if (_playerAudio) {
        _playerAudio.pause();
        _playerAudio.removeAttribute('src');
        _playerAudio.load();
    }
    _playerStreamId = null;
    _updateListenIcons(null);
}

function _updateListenIcons(activeId) {
    document.querySelectorAll('.btn-listen').forEach(function(el) {
        var sid = parseInt(el.getAttribute('data-stream-id'));
        if (sid === activeId) {
            el.classList.add('listening');
        } else {
            el.classList.remove('listening');
        }
    });
}

// Intercept start/stop form submissions via AJAX (prevent page reload)
document.addEventListener('submit', function(e) {
    var form = e.target;
    if (!form.querySelector('.btn-start') && !form.querySelector('.btn-stop')) return;
    e.preventDefault();
    var btn = form.querySelector('button');
    btn.disabled = true;
    btn.textContent = '...';
    fetch(form.action, {method: 'POST', credentials: 'include', headers: {'X-Requested-With': 'XMLHttpRequest'}})
        .then(function() { btn.disabled = false; updateStatus(); })
        .catch(function() { btn.disabled = false; updateStatus(); });
});

// --- Start All / Stop All ---
function startAll(btn) {
    btn.disabled = true;
    btn.textContent = '...';
    fetch('/api/start-all', {method: 'POST', credentials: 'include', headers: {'X-Requested-With': 'XMLHttpRequest'}})
        .then(r => r.json())
        .then(data => {
            btn.textContent = data.started + ' ' + t('dash.started');
            setTimeout(() => { btn.textContent = 'Start All'; btn.disabled = false; }, 2000);
            updateStatus();
        })
        .catch(() => { btn.textContent = 'Start All'; btn.disabled = false; });
}

function stopAll(btn) {
    if (!confirm(t('dash.stop_all_confirm'))) return;
    btn.disabled = true;
    btn.textContent = '...';
    fetch('/api/stop-all', {method: 'POST', credentials: 'include', headers: {'X-Requested-With': 'XMLHttpRequest'}})
        .then(r => r.json())
        .then(data => {
            btn.textContent = data.stopped + ' ' + t('dash.stopped');
            setTimeout(() => { btn.textContent = 'Stop All'; btn.disabled = false; }, 2000);
            updateStatus();
        })
        .catch(() => { btn.textContent = 'Stop All'; btn.disabled = false; });
}

// --- Cast to device ---
var _castMenu = null;
var _castDevicesCache = null;
var _castActiveCache = {};

function toggleCastMenu(btn, streamId) {
    // Close existing menu
    if (_castMenu) {
        _castMenu.remove();
        _castMenu = null;
        return;
    }
    // Show loading menu
    var menu = document.createElement('div');
    menu.className = 'cast-menu';
    menu.innerHTML = '<div class="cast-menu-loading">' + t('cast.searching') + '</div>';
    // Position: prefer below button, flip above if not enough space
    var rect = btn.getBoundingClientRect();
    menu.style.position = 'fixed';
    menu.style.left = rect.left + 'px';
    menu.style.visibility = 'hidden';
    document.body.appendChild(menu);
    _castMenu = menu;

    _repositionCastMenu(menu, rect);

    // Fetch devices
    fetch('/api/cast/devices?refresh=1', {credentials: 'include'})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (!_castMenu) return;
            _castDevicesCache = data.devices || [];
            _castActiveCache = data.active_casts || {};
            _renderCastMenu(menu, streamId);
            // Reposition after content changed
            _repositionCastMenu(menu, rect);
        })
        .catch(function() {
            if (!_castMenu) return;
            menu.innerHTML = '<div class="cast-menu-empty">' + t('cast.load_error') + '</div>';
        });

    // Close on outside click
    setTimeout(function() {
        document.addEventListener('click', _closeCastMenuOutside);
    }, 10);
}

function _repositionCastMenu(menu, btnRect) {
    requestAnimationFrame(function() {
        var menuH = menu.offsetHeight;
        var availableBelow = window.innerHeight - 60 - btnRect.bottom - 4;
        if (availableBelow >= menuH) {
            menu.style.top = btnRect.bottom + 2 + 'px';
        } else {
            menu.style.top = Math.max(4, btnRect.top - menuH - 2) + 'px';
        }
        var menuW = menu.offsetWidth;
        var left = btnRect.left;
        if (left + menuW > window.innerWidth - 8) {
            left = window.innerWidth - menuW - 8;
        }
        menu.style.left = Math.max(4, left) + 'px';
        menu.style.visibility = '';
    });
}

function _closeCastMenuOutside(e) {
    if (_castMenu && !_castMenu.contains(e.target) && !e.target.classList.contains('btn-cast') && !e.target.classList.contains('btn-cast-detail') && !e.target.closest('.btn-cast')) {
        _castMenu.remove();
        _castMenu = null;
        document.removeEventListener('click', _closeCastMenuOutside);
    }
}

function _renderCastMenu(menu, streamId) {
    var html = '';
    var activeDeviceId = _castActiveCache[streamId];
    var hasDevices = false;

    if (_castDevicesCache.length === 0) {
        html = '<div class="cast-menu-empty">' + t('cast.no_devices') + '</div>';
    } else {
        _castDevicesCache.forEach(function(d) {
            hasDevices = true;
            var isActive = (d.id === activeDeviceId);
            var isEnabled = d.enabled !== false;
            var cls = 'cast-menu-item';
            if (!isEnabled) cls += ' disabled';
            if (isActive) cls += ' casting-active';

            var badge = d.type === 'lms'
                ? '<span class="cast-device-badge badge-lms">LMS</span>'
                : '<span class="cast-device-badge badge-sonos">Sonos</span>';

            var label = d.name + badge;
            if (!isEnabled) label += '<span class="cast-device-type"> (' + t('cast.disabled') + ')</span>';
            if (isActive) label = '&#9654; ' + label;
            if (d.model) label += '<span class="cast-device-type"> ' + d.model + '</span>';

            html += '<div class="cast-menu-device">';
            html += '<button class="' + cls + '" data-device-id="' + d.id + '" data-enabled="' + isEnabled + '" onclick="castToDevice(' + streamId + ', \'' + d.id + '\', ' + isEnabled + ')">&#9654; ' + t('cast.play') + '' + label + '</button>';
            if (isEnabled) {
                html += '<button class="cast-menu-item cast-menu-queue-add" onclick="addToQueue(' + streamId + ', \'' + d.id + '\')">' + t('cast.queue_add') + '' + d.name + '</button>';
            }
            html += '</div>';
        });
    }

    // Stop button if actively casting
    if (activeDeviceId) {
        html += '<button class="cast-menu-item cast-menu-stop" onclick="stopCast(' + streamId + ')">' + t('cast.stop_playback') + '</button>';
    }

    menu.innerHTML = html;
}

function castToDevice(streamId, deviceId, enabled) {
    if (!enabled) return;
    if (_castMenu) { _castMenu.remove(); _castMenu = null; }

    // Check max cast limit client-side
    var activeSids = Object.keys(_castActiveCache).filter(function(sid) {
        return parseInt(sid) !== streamId;
    });
    if (activeSids.length >= MAX_CASTS) {
        alert(t('cast.max_reached'));
        return;
    }

    // Remove any previous cast on the same device (switch stream)
    Object.keys(_castActiveCache).forEach(function(sid) {
        if (_castActiveCache[sid] === deviceId) {
            delete _castActiveCache[sid];
        }
    });

    fetch('/api/cast/play', {
        method: 'POST',
        credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({stream_id: streamId, device_id: deviceId}),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.success) {
            _castActiveCache[streamId] = deviceId;
            _updateCastIcons();
            _updatePlayerBar();
        } else {
            alert(data.message || t('cast.failed'));
        }
    })
    .catch(function() { alert(t('cast.failed')); });
}

function stopCast(streamId) {
    if (_castMenu) { _castMenu.remove(); _castMenu = null; }

    fetch('/api/cast/stop', {
        method: 'POST',
        credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({stream_id: streamId}),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.success) {
            delete _castActiveCache[streamId];
            _updateCastIcons();
        }
    })
    .catch(function() {});
}

function _updateCastIcons() {
    document.querySelectorAll('.btn-cast').forEach(function(el) {
        var sid = parseInt(el.getAttribute('data-stream-id'));
        if (_castActiveCache[sid]) {
            el.classList.add('casting');
        } else {
            el.classList.remove('casting');
        }
    });
}

// --- Multi-Cast Player Bars ---
var _playerData = null;
var _playerVolumeDebounce = {};  // deviceId -> timeout
var _playerVolumeLocal = {};     // deviceId -> value or true
var _multiroomOpen = false;
var _lastStreamStatus = {};
var _volState = {};              // deviceId -> {value, dragging}
var _volStepResetTimers = {};
var MAX_CASTS = 4;

function _updatePlayerBar() {
    fetch('/api/cast/player', {credentials: 'include'})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            _playerData = data;
            _refreshPlayerBar();
        })
        .catch(function() {});
}

function _getVolState(deviceId) {
    if (!_volState[deviceId]) _volState[deviceId] = {value: 50, dragging: false};
    return _volState[deviceId];
}

function _renderPlayerHTML(p, idx) {
    var st = _lastStreamStatus[p.stream_id] || {};
    var castTrack = st.current_track || p.current_track || '';
    var castHasTrack = castTrack && castTrack.replace(/[\s\-]/g, '') !== '';
    var castCover = st.cover_url || p.cover_url || null;
    var vol = _getVolState(p.device_id);
    var curVol = (_playerVolumeLocal[p.device_id] != null && _playerVolumeLocal[p.device_id] !== true)
        ? _playerVolumeLocal[p.device_id] : (p.volume != null ? p.volume : 50);

    var coverHtml = castCover
        ? '<img src="' + castCover + '" alt="">'
        : '<div class="player-cover-placeholder"><svg width="20" height="20" viewBox="0 0 24 24" fill="#555"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55C7.79 13 6 14.79 6 17s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg></div>';

    var html = '<div class="player-bar" data-player-idx="' + idx + '">'
        + '<div class="player-bar-inner">'
        + '<div class="player-cover-wrap">' + coverHtml + '</div>'
        + '<div class="player-info">'
        + '<div class="player-track">' + (castHasTrack ? _escHtmlPlayer(castTrack) : t('player.waiting_track')) + '</div>'
        + '<div class="player-stream">' + _escHtmlPlayer(p.stream_name) + '</div>'
        + '</div>'
        + '<div class="player-controls">'
        + '<button class="player-btn" onclick="playerStop(' + p.stream_id + ')" title="' + t('player.stop_title') + '">'
        + '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>'
        + '</button>'
        + '</div>'
        + '<div class="player-volume">'
        + '<svg width="14" height="14" viewBox="0 0 24 24" fill="#888" style="flex-shrink:0;"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/></svg>'
        + '<button class="player-vol-btn" onclick="playerVolumeStep(\'' + p.device_id + '\', -2)">&#8722;</button>'
        + '<div class="vol-slider" data-device-id="' + p.device_id + '">'
        + '<div class="vol-slider-track"></div>'
        + '<div class="vol-slider-fill" style="width:' + curVol + '%"></div>'
        + '<div class="vol-slider-handle" style="left:' + curVol + '%"></div>'
        + '</div>'
        + '<button class="player-vol-btn" onclick="playerVolumeStep(\'' + p.device_id + '\', 2)">+</button>'
        + '<span class="player-volume-value">' + curVol + '</span>'
        + '</div>';

    // Multiroom only on first player
    if (idx === 0) {
        html += '<div class="player-multiroom">'
            + '<button class="player-btn player-multiroom-btn' + (_multiroomGroupCount > 1 ? ' has-group' : '') + '" onclick="toggleMultiroomPanel()" title="Multiroom">'
            + '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M18.2 1C15.53 1 13.27 2.56 12.34 4.78l-1.55-.64C11.91 1.33 14.79-1 18.2-1c4.42 0 8 3.58 8 8 0 3.41-2.33 6.29-5.14 7.41l-.64-1.55C22.64 11.73 24.2 9.47 24.2 7c0-3.31-2.69-6-6-6z" transform="scale(0.7) translate(5,5)"/><circle cx="12" cy="12" r="3"/><path d="M7 12c0-2.76 2.24-5 5-5v-2c-3.87 0-7 3.13-7 7s3.13 7 7 7v-2c-2.76 0-5-2.24-5-5z"/></svg>'
            + '</button>'
            + '<div id="multiroom-panel" style="display:none;"></div>'
            + '</div>';
    }

    html += '<div class="player-device-name">' + _escHtmlPlayer(p.device_name) + '</div>'
        + '</div></div>';
    return html;
}

var _multiroomGroupCount = 0;

function _refreshPlayerBar() {
    var container = document.getElementById('player-container');
    if (!container) return;

    var hasCast = _playerData && _playerData.active && _playerData.players && _playerData.players.length > 0;

    if (!hasCast) {
        container.innerHTML = '<div class="player-bar">'
            + '<div class="player-bar-inner">'
            + '<div class="player-cover-wrap"><div class="player-cover-placeholder"><svg width="20" height="20" viewBox="0 0 24 24" fill="#555"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55C7.79 13 6 14.79 6 17s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg></div></div>'
            + '<div class="player-info"><div class="player-track">' + t('player.no_cast') + '</div><div class="player-stream"></div></div>'
            + '</div></div>';
        _updateVersionPosition(1);
        _initVolSliders();
        return;
    }

    // Multiroom group count
    _multiroomGroupCount = 0;
    if (_playerData.speakers) {
        _playerData.speakers.forEach(function(s) { if (s.active_for) _multiroomGroupCount++; });
    }

    var players = _playerData.players.slice(0, MAX_CASTS);
    var html = '';
    players.forEach(function(p, idx) {
        html += _renderPlayerHTML(p, idx);
    });
    container.innerHTML = html;

    // Update volume sliders from server data (only if not dragging/stepping)
    players.forEach(function(p) {
        var vs = _getVolState(p.device_id);
        if (!vs.dragging && _playerVolumeLocal[p.device_id] == null) {
            var vol = p.volume != null ? p.volume : 50;
            _setVolSlider(p.device_id, vol);
        }
    });

    _updateVersionPosition(players.length);
    _initVolSliders();

    if (_multiroomOpen) _renderMultiroomPanel(_playerData);
}

function _updateVersionPosition(playerCount) {
    var vi = document.getElementById('version-info');
    var totalH = playerCount * 70;
    if (vi) vi.style.bottom = (totalH + 4) + 'px';
    var mainEl = document.querySelector('main.container');
    if (mainEl) mainEl.style.paddingBottom = (totalH + 20) + 'px';
}

function _escHtmlPlayer(s) {
    if (!s) return '';
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

function playerStop(streamId) {
    fetch('/api/cast/stop', {
        method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({stream_id: parseInt(streamId)}),
    }).then(function() {
        delete _castActiveCache[streamId];
        _updateCastIcons();
        _updatePlayerBar();
    });
}

// --- Per-player volume control ---

function _setVolSlider(deviceId, val) {
    val = Math.max(0, Math.min(100, Math.round(val)));
    _getVolState(deviceId).value = val;
    var slider = document.querySelector('.vol-slider[data-device-id="' + deviceId + '"]');
    if (!slider) return;
    var fill = slider.querySelector('.vol-slider-fill');
    var handle = slider.querySelector('.vol-slider-handle');
    if (fill) fill.style.width = val + '%';
    if (handle) handle.style.left = val + '%';
}

function _volFromEvent(e, deviceId) {
    var slider = document.querySelector('.vol-slider[data-device-id="' + deviceId + '"]');
    if (!slider) return _getVolState(deviceId).value;
    var rect = slider.getBoundingClientRect();
    var x = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
    return Math.max(0, Math.min(100, Math.round(x / rect.width * 100)));
}

function _volCommit(deviceId, val) {
    _setVolSlider(deviceId, val);
    // Update value display
    var slider = document.querySelector('.vol-slider[data-device-id="' + deviceId + '"]');
    if (slider) {
        var valSpan = slider.parentNode.querySelector('.player-volume-value');
        if (valSpan) valSpan.textContent = val;
    }
    clearTimeout(_playerVolumeDebounce[deviceId]);
    _playerVolumeDebounce[deviceId] = setTimeout(function() {
        fetch('/api/cast/volume/' + encodeURIComponent(deviceId), {
            method: 'POST', credentials: 'include',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({volume: val}),
        }).catch(function() {});
    }, 100);
}

function playerVolumeStep(deviceId, delta) {
    var vs = _getVolState(deviceId);
    var newVal = Math.max(0, Math.min(100, vs.value + delta));
    _playerVolumeLocal[deviceId] = newVal;
    _volCommit(deviceId, newVal);
    clearTimeout(_volStepResetTimers[deviceId]);
    _volStepResetTimers[deviceId] = setTimeout(function() { _playerVolumeLocal[deviceId] = null; }, 2000);
}

// Attach drag listeners to all volume sliders (called after each render)
var _activeVolDrag = null; // deviceId string

function _initVolSliders() {
    document.querySelectorAll('.vol-slider').forEach(function(slider) {
        var deviceId = slider.getAttribute('data-device-id');
        if (!deviceId) return;

        slider.addEventListener('mousedown', function(e) {
            e.preventDefault();
            _activeVolDrag = deviceId;
            _getVolState(deviceId).dragging = true;
            _playerVolumeLocal[deviceId] = true;
            var val = _volFromEvent(e, deviceId);
            _setVolSlider(deviceId, val);
            var valSpan = slider.parentNode.querySelector('.player-volume-value');
            if (valSpan) valSpan.textContent = val;
        });
        slider.addEventListener('touchstart', function(e) {
            e.preventDefault();
            _activeVolDrag = deviceId;
            _getVolState(deviceId).dragging = true;
            _playerVolumeLocal[deviceId] = true;
            var val = _volFromEvent(e, deviceId);
            _setVolSlider(deviceId, val);
            var valSpan = slider.parentNode.querySelector('.player-volume-value');
            if (valSpan) valSpan.textContent = val;
        }, {passive: false});
    });
}

document.addEventListener('mousemove', function(e) {
    if (!_activeVolDrag) return;
    var val = _volFromEvent(e, _activeVolDrag);
    _setVolSlider(_activeVolDrag, val);
    var slider = document.querySelector('.vol-slider[data-device-id="' + _activeVolDrag + '"]');
    if (slider) {
        var valSpan = slider.parentNode.querySelector('.player-volume-value');
        if (valSpan) valSpan.textContent = val;
    }
});
document.addEventListener('touchmove', function(e) {
    if (!_activeVolDrag) return;
    var val = _volFromEvent(e, _activeVolDrag);
    _setVolSlider(_activeVolDrag, val);
    var slider = document.querySelector('.vol-slider[data-device-id="' + _activeVolDrag + '"]');
    if (slider) {
        var valSpan = slider.parentNode.querySelector('.player-volume-value');
        if (valSpan) valSpan.textContent = val;
    }
}, {passive: false});
document.addEventListener('mouseup', function() {
    if (!_activeVolDrag) return;
    var deviceId = _activeVolDrag;
    _getVolState(deviceId).dragging = false;
    _playerVolumeLocal[deviceId] = null;
    _volCommit(deviceId, _getVolState(deviceId).value);
    _activeVolDrag = null;
});
document.addEventListener('touchend', function() {
    if (!_activeVolDrag) return;
    var deviceId = _activeVolDrag;
    _getVolState(deviceId).dragging = false;
    _playerVolumeLocal[deviceId] = null;
    _volCommit(deviceId, _getVolState(deviceId).value);
    _activeVolDrag = null;
});

// Multiroom panel
function toggleMultiroomPanel() {
    _multiroomOpen = !_multiroomOpen;
    var panel = document.getElementById('multiroom-panel');
    if (_multiroomOpen && _playerData) {
        _renderMultiroomPanel(_playerData);
        panel.style.display = '';
    } else {
        panel.style.display = 'none';
    }
    // Close on outside click
    if (_multiroomOpen) {
        setTimeout(function() {
            document.addEventListener('click', _closeMultiroomOutside);
        }, 10);
    }
}

function _closeMultiroomOutside(e) {
    var panel = document.getElementById('multiroom-panel');
    var btn = document.querySelector('.player-multiroom-btn');
    if (panel && !panel.contains(e.target) && (!btn || !btn.contains(e.target))) {
        _multiroomOpen = false;
        panel.style.display = 'none';
        document.removeEventListener('click', _closeMultiroomOutside);
    }
}

function _renderMultiroomPanel(data) {
    var panel = document.getElementById('multiroom-panel');
    if (!panel || !data.players || data.players.length === 0) return;

    var masterDeviceId = data.players[0].device_id;
    var masterType = data.players[0].device_type;
    var speakers = data.speakers || [];

    var html = '';
    speakers.forEach(function(s) {
        // Only show speakers of the same type as master
        if (s.type !== masterType) return;
        var isActive = !!s.active_for;
        var isMaster = (s.id === masterDeviceId);
        var checkCls = 'multiroom-check' + (isActive ? ' checked' : '');
        var badge = s.type === 'lms'
            ? '<span class="multiroom-speaker-badge badge-lms">LMS</span>'
            : '<span class="multiroom-speaker-badge badge-sonos">Sonos</span>';
        html += '<div class="multiroom-speaker" onclick="toggleMultiroomSpeaker(\'' + s.id + '\', ' + isActive + ', ' + isMaster + ')">';
        html += '<div class="' + checkCls + '"></div>';
        html += '<span class="multiroom-speaker-name">' + s.name + (isMaster ? ' (' + t('player.master') + ')' : '') + '</span>';
        html += badge;
        html += '</div>';
    });

    if (!html) html = '<div style="padding:0.5rem 0.8rem;color:#888;">' + t('player.multiroom_empty') + '</div>';
    panel.innerHTML = html;
}

function toggleMultiroomSpeaker(speakerId, isActive, isMaster) {
    if (isMaster) return; // Can't remove master

    var masterDeviceId = _playerData.players[0].device_id;

    if (isActive) {
        // Remove from group
        fetch('/api/cast/multiroom/remove', {
            method: 'POST', credentials: 'include',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({device_id: speakerId}),
        }).then(function() { _updatePlayerBar(); });
    } else {
        // Add to group
        fetch('/api/cast/multiroom/add', {
            method: 'POST', credentials: 'include',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({master_device_id: masterDeviceId, slave_device_id: speakerId}),
        }).then(function() { _updatePlayerBar(); });
    }
}

// --- Cast queue functions ---

function addToQueue(streamId, deviceId) {
    if (_castMenu) { _castMenu.remove(); _castMenu = null; }
    fetch('/api/cast/queue/' + encodeURIComponent(deviceId) + '/add', {
        method: 'POST',
        credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({stream_id: streamId}),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.success) {
            _renderQueuePanel();
        } else {
            alert(data.error || t('general.error'));
        }
    })
    .catch(function() { alert(t('queue.add_error')); });
}

function removeFromQueue(deviceId, index) {
    fetch('/api/cast/queue/' + encodeURIComponent(deviceId) + '/' + index, {
        method: 'DELETE',
        credentials: 'include',
    })
    .then(function(r) { return r.json(); })
    .then(function() { _renderQueuePanel(); })
    .catch(function() {});
}

function advanceQueue(deviceId) {
    fetch('/api/cast/queue/' + encodeURIComponent(deviceId) + '/next', {
        method: 'POST',
        credentials: 'include',
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (!data.success) {
            alert(data.error || t('queue.empty'));
        }
        _renderQueuePanel();
    })
    .catch(function() {});
}

function setQueueTimer(deviceId) {
    var input = document.querySelector('.queue-timer-input[data-device-id="' + deviceId + '"]');
    var minutes = input ? parseInt(input.value) : 0;
    fetch('/api/cast/queue/' + encodeURIComponent(deviceId) + '/timer', {
        method: 'POST',
        credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({minutes: minutes}),
    })
    .then(function(r) { return r.json(); })
    .then(function() { _renderQueuePanel(); })
    .catch(function() {});
}

function cancelQueueTimer(deviceId) {
    fetch('/api/cast/queue/' + encodeURIComponent(deviceId) + '/timer', {
        method: 'POST',
        credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({minutes: 0}),
    })
    .then(function(r) { return r.json(); })
    .then(function() { _renderQueuePanel(); })
    .catch(function() {});
}

function clearQueue(deviceId) {
    fetch('/api/cast/queue/' + encodeURIComponent(deviceId), {
        method: 'DELETE',
        credentials: 'include',
    })
    .then(function(r) { return r.json(); })
    .then(function() { _renderQueuePanel(); })
    .catch(function() {});
}

function _renderQueuePanel() {
    var container = document.getElementById('queue-panel-content');
    if (!container) return;

    fetch('/api/cast/queues', {credentials: 'include'})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var keys = Object.keys(data);
            var panel = document.getElementById('queue-panel');
            if (keys.length === 0) {
                if (panel) panel.style.display = 'none';
                container.innerHTML = '';
                return;
            }
            if (panel) panel.style.display = '';

            var html = '';
            keys.forEach(function(deviceId) {
                var info = data[deviceId];
                html += '<div class="queue-device-section">';
                html += '<div class="queue-device-header">' + info.device_name + '</div>';

                info.queue.forEach(function(item, idx) {
                    html += '<div class="queue-item">';
                    html += '<span class="queue-item-pos">' + (idx + 1) + '.</span>';
                    html += '<span class="queue-item-name">' + item.name + '</span>';
                    html += '<button class="queue-item-remove" onclick="removeFromQueue(\'' + deviceId + '\', ' + idx + ')" title="' + t('general.remove_title') + '">&#10005;</button>';
                    html += '</div>';
                });

                html += '<div class="queue-controls">';
                html += '<button class="btn-icon outline queue-btn" onclick="advanceQueue(\'' + deviceId + '\')">' + t('queue.next') + '</button>';
                html += '<button class="btn-icon outline queue-btn queue-btn-clear" onclick="clearQueue(\'' + deviceId + '\')">' + t('queue.clear') + '</button>';

                if (info.timer) {
                    html += '<span class="queue-timer-info">' + t('queue.timer') + '' + info.timer.remaining + ' ' + t('queue.min_remaining') + '</span>';
                    html += '<button class="btn-icon outline queue-btn queue-btn-timer-stop" onclick="cancelQueueTimer(\'' + deviceId + '\')">' + t('queue.stop_timer') + '</button>';
                } else {
                    html += '<input type="number" class="queue-timer-input" data-device-id="' + deviceId + '" value="30" min="1" max="999" title="' + t('general.minutes_title') + '">';
                    html += '<button class="btn-icon outline queue-btn" onclick="setQueueTimer(\'' + deviceId + '\')">' + t('queue.set_timer') + '</button>';
                }

                html += '</div>';
                html += '</div>';
            });
            container.innerHTML = html;
        })
        .catch(function() {});
}

// Refresh active casts and player bar on status poll
var _origUpdateStatus = updateStatus;
updateStatus = function() {
    _origUpdateStatus();
    // Sync cast state
    fetch('/api/cast/devices', {credentials: 'include'})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            _castActiveCache = data.active_casts || {};
            _castDevicesCache = data.devices || _castDevicesCache;
            _updateCastIcons();
        })
        .catch(function() {});
    // Update player bar
    _updatePlayerBar();
};

// Start polling
setInterval(updateStatus, 5000);
updateStatus();
