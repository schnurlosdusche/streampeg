// Poll status every 5 seconds
function updateStatus() {
    fetch('/api/status', {credentials: 'include'})
    .then(r => r.json())
    .then(data => {
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

                const sizeCell = row.querySelector('.size-cell');
                if (sizeCell) {
                    sizeCell.innerHTML = s.disk_usage_mb > 0 ? (s.disk_usage_mb / 1024).toFixed(1) + ' <small style="opacity:0.6">GB</small>' : '-';
                }

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

// --- Browser listen (persistent across page navigation via localStorage) ---
var _playerAudio = null;
var _playerStreamId = null;
var _playerStreamUrl = null;
var _browserVolume = parseFloat(localStorage.getItem('_browserVolume') || '0.32');
var _browserPaused = false;
var _browserIcyTrack = '';
var _browserIcyCover = null;
var _isLibraryTrack = false;
var _seekDragging = false;
var _waveformData = null; // Float32Array of peak values for current track
var _waveformTrackUrl = null;

function _initBrowserAudio() {
    if (!_playerAudio) {
        _playerAudio = new Audio();
        _playerAudio.crossOrigin = 'anonymous';
        _playerAudio.volume = _browserVolume;
        _playerAudio.addEventListener('error', function() { stopListen(); });
        _playerAudio.addEventListener('timeupdate', _onSeekUpdate);
        _playerAudio.addEventListener('ended', function() {
            _isLibraryTrack = false;
            stopListen();
        });
    }
}

function toggleListen(streamId, url) {
    _initBrowserAudio();
    if (_playerStreamId === streamId) {
        stopListen();
        return;
    }
    _waveformData = null;
    _playerAudio.pause();
    _playerAudio.removeAttribute('src');
    _playerAudio.load();
    _isLibraryTrack = false;
    _playerStreamId = streamId;
    _playerStreamUrl = url;
    _playerAudio.volume = _browserVolume;
    _playerAudio.src = url;
    _playerAudio.play();
    // Persist in localStorage
    localStorage.setItem('_listenStreamId', streamId);
    localStorage.setItem('_listenStreamUrl', url);
    _updateListenIcons(streamId);
    _refreshPlayerBar();
}

function stopListen() {
    _waveformData = null;
    if (_playerAudio) {
        _playerAudio.pause();
        _playerAudio.removeAttribute('src');
        _playerAudio.load();
    }
    _playerStreamId = null;
    _playerStreamUrl = null;
    _isLibraryTrack = false;
    localStorage.removeItem('_listenStreamId');
    localStorage.removeItem('_listenStreamUrl');
    _updateListenIcons(null);
    _refreshPlayerBar();
}

function _restoreListenState() {
    var savedId = localStorage.getItem('_listenStreamId');
    var savedUrl = localStorage.getItem('_listenStreamUrl');
    if (savedId && savedUrl) {
        _initBrowserAudio();
        _playerStreamId = parseInt(savedId);
        _playerStreamUrl = savedUrl;
        _playerAudio.volume = _browserVolume;
        _playerAudio.src = savedUrl;
        _playerAudio.play().catch(function() {});
        _updateListenIcons(_playerStreamId);
    }
}

function setBrowserVolume(val) {
    _browserVolume = Math.max(0, Math.min(1, val));
    localStorage.setItem('_browserVolume', _browserVolume);
    if (_playerAudio) _playerAudio.volume = _browserVolume;
}

function toggleBrowserPause() {
    if (!_playerAudio || !_playerStreamId) return;
    if (_browserPaused) {
        _playerAudio.src = _playerStreamUrl;
        _playerAudio.play().catch(function() {});
        _browserPaused = false;
    } else {
        _playerAudio.pause();
        _playerAudio.removeAttribute('src');
        _playerAudio.load();
        _browserPaused = true;
    }
    _refreshPlayerBar();
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
    var activeDeviceIds = _castActiveCache[streamId] || [];
    var hasDevices = false;

    if (_castDevicesCache.length === 0) {
        html = '<div class="cast-menu-empty">' + t('cast.no_devices') + '</div>';
    } else {
        _castDevicesCache.forEach(function(d) {
            hasDevices = true;
            var isActive = activeDeviceIds.indexOf(d.id) !== -1;
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
    if (activeDeviceIds.length > 0) {
        html += '<button class="cast-menu-item cast-menu-stop" onclick="stopCast(' + streamId + ')">' + t('cast.stop_playback') + '</button>';
    }

    menu.innerHTML = html;
}

function castToDevice(streamId, deviceId, enabled) {
    if (!enabled) return;
    if (_castMenu) { _castMenu.remove(); _castMenu = null; }

    // Check max cast limit client-side (count total active devices)
    var totalActive = 0;
    Object.keys(_castActiveCache).forEach(function(sid) {
        totalActive += (_castActiveCache[sid] || []).length;
    });
    if (totalActive >= MAX_CASTS) {
        alert(t('cast.max_reached'));
        return;
    }

    // Remove device from any other stream's list (device can only play one stream)
    Object.keys(_castActiveCache).forEach(function(sid) {
        var arr = _castActiveCache[sid] || [];
        var idx = arr.indexOf(deviceId);
        if (idx !== -1) arr.splice(idx, 1);
        if (arr.length === 0) delete _castActiveCache[sid];
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
            if (!_castActiveCache[streamId]) _castActiveCache[streamId] = [];
            if (_castActiveCache[streamId].indexOf(deviceId) === -1) {
                _castActiveCache[streamId].push(deviceId);
            }
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
        if (_castActiveCache[sid] && _castActiveCache[sid].length > 0) {
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
var MAX_PLAYERS = 5; // 4 cast + 1 browser

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
    // Prefer player API data (includes ICY) over SSE status (only has recording data)
    var castTrack = p.current_track || st.current_track || '';
    var castHasTrack = castTrack && castTrack.replace(/[\s\-]/g, '') !== '';
    var castCover = p.cover_url || st.cover_url || null;
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
        + '<div class="player-volume">';

    var pauseKey = p.stream_id + ':' + p.device_id;
    var isPaused = !!_pausedStreams[pauseKey];
    if (isPaused) {
        // Show play button (resume)
        html += '<button class="player-btn" onclick="playerPause(' + p.stream_id + ',\'' + p.device_id + '\')" title="Play">'
            + '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>'
            + '</button>';
    } else {
        // Show pause button
        html += '<button class="player-btn" onclick="playerPause(' + p.stream_id + ',\'' + p.device_id + '\')" title="' + t('player.pause_title') + '">'
            + '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="3" width="4" height="18" rx="1"/><rect x="15" y="3" width="4" height="18" rx="1"/></svg>'
            + '</button>';
    }

    html += '<button class="player-btn" onclick="playerStop(' + p.stream_id + ',\'' + p.device_id + '\')" title="' + t('player.stop_title') + '">'
        + '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>'
        + '</button>'
        + '<button class="player-vol-btn" style="margin-left:0.5rem;" onclick="playerVolumeStep(\'' + p.device_id + '\', -2)">&#8722;</button>'
        + '<div class="vol-slider" data-device-id="' + p.device_id + '">'
        + '<div class="vol-slider-track"></div>'
        + '<div class="vol-slider-fill" style="width:' + curVol + '%"></div>'
        + '<div class="vol-slider-handle" style="left:' + curVol + '%"></div>'
        + '</div>'
        + '<button class="player-vol-btn" onclick="playerVolumeStep(\'' + p.device_id + '\', 2)">+</button>'
        + '<span class="player-volume-value">' + curVol + '</span>'
        + '</div>'
        + '<div class="player-right">';

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
        + '</div>'
        + '</div></div>';
    return html;
}

var _multiroomGroupCount = 0;

function _renderBrowserPlayerHTML() {
    var st = _lastStreamStatus[_playerStreamId] || {};
    // Use ICY data if available (fetched independently), fall back to recording status
    var trackName = _browserIcyTrack || st.current_track || '';
    var hasTrack = trackName && trackName !== 'recording' && trackName !== '-' && trackName.replace(/[\s\-]/g, '') !== '';
    var coverUrl = _browserIcyCover || st.cover_url || null;
    var streamName = st.stream_name || ('Stream ' + _playerStreamId);
    var volPct = Math.round(_browserVolume * 100);

    var coverHtml = coverUrl
        ? '<img src="' + coverUrl + '" alt="">'
        : '<div class="player-cover-placeholder"><svg width="20" height="20" viewBox="0 0 24 24" fill="#555"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55C7.79 13 6 14.79 6 17s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg></div>';

    // Seek bar with waveform for library tracks (finite duration)
    var seekHtml = '';
    if (_isLibraryTrack && _playerAudio) {
        var dur = _playerAudio.duration || 0;
        var cur = _playerAudio.currentTime || 0;
        var seekPct = (dur && isFinite(dur)) ? (cur / dur * 100) : 0;
        seekHtml = '<div class="player-seek-row">'
            + '<span class="player-seek-time" id="seek-time">' + _fmtTime(cur) + '</span>'
            + '<div class="player-seek-bar" id="seek-bar">'
            + '<canvas id="waveform-canvas" class="player-waveform" width="600" height="32"></canvas>'
            + '<div class="player-seek-overlay" id="seek-fill" style="width:' + seekPct + '%"></div>'
            + '<div class="player-seek-handle" id="seek-handle" style="left:' + seekPct + '%"></div>'
            + '</div>'
            + '<span class="player-seek-time">' + _fmtTime(dur) + '</span>'
            + '</div>';
    }

    var html = '<div class="player-bar player-bar-browser' + (_isLibraryTrack ? ' player-bar-library' : '') + '">';

    if (_isLibraryTrack) {
        // Grid layout: cover left spanning 2 rows, top = track + controls + volume, bottom = seek/waveform
        html += '<div class="player-cover-wrap">' + coverHtml + '</div>'
            + '<div class="player-lib-top">'
            + '<div class="player-info">'
            + '<div class="player-track">' + (hasTrack ? _escHtmlPlayer(trackName) : '') + '</div>'
            + '</div>'
            + '<div class="player-volume">';
    } else {
        html += '<div class="player-bar-inner">'
            + '<div class="player-cover-wrap">' + coverHtml + '</div>'
            + '<div class="player-info">'
            + '<div class="player-track">' + (hasTrack ? _escHtmlPlayer(trackName) : t('player.waiting_track')) + '</div>'
            + '<div class="player-stream">' + _escHtmlPlayer(streamName) + '</div>'
            + '</div>'
            + '<div class="player-volume">';
    }

    if (_browserPaused) {
        html += '<button class="player-btn" onclick="toggleBrowserPause()" title="Play">'
            + '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>'
            + '</button>';
    } else {
        html += '<button class="player-btn" onclick="toggleBrowserPause()" title="' + t('player.pause_title') + '">'
            + '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="3" width="4" height="18" rx="1"/><rect x="15" y="3" width="4" height="18" rx="1"/></svg>'
            + '</button>';
    }

    html += '<button class="player-btn" onclick="stopListen()" title="' + t('player.stop_title') + '">'
        + '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>'
        + '</button>'
        + '<button class="player-vol-btn" style="margin-left:0.5rem;" onclick="_browserVolumeStep(-5)">&#8722;</button>'
        + '<div class="vol-slider" data-device-id="browser">'
        + '<div class="vol-slider-track"></div>'
        + '<div class="vol-slider-fill" style="width:' + volPct + '%"></div>'
        + '<div class="vol-slider-handle" style="left:' + volPct + '%"></div>'
        + '</div>'
        + '<button class="player-vol-btn" onclick="_browserVolumeStep(5)">+</button>'
        + '<span class="player-volume-value">' + volPct + '</span>'
        + '</div>'
        + '<div class="player-right">'
        + '<div class="player-device-name">' + t('player.browser') + '</div>'
        + '</div>';

    if (_isLibraryTrack) {
        // Close top row, add seek bar as second grid row
        html += '</div>' + seekHtml + '</div>';
    } else {
        html += '</div></div>';
    }
    return html;
}

function _browserVolumeStep(delta) {
    var newPct = Math.max(0, Math.min(100, Math.round(_browserVolume * 100) + delta));
    setBrowserVolume(newPct / 100);
    _setVolSlider('browser', newPct);
    var valSpan = document.querySelector('.vol-slider[data-device-id="browser"]');
    if (valSpan) {
        var span = valSpan.parentNode.querySelector('.player-volume-value');
        if (span) span.textContent = newPct;
    }
}

// --- Seek slider for library tracks ---
function _onSeekUpdate() {
    if (_seekDragging || !_isLibraryTrack || !_playerAudio) return;
    var dur = _playerAudio.duration || 0;
    var cur = _playerAudio.currentTime || 0;
    if (!dur || !isFinite(dur)) return;
    var pct = (cur / dur) * 100;
    var fill = document.getElementById('seek-fill');
    var handle = document.getElementById('seek-handle');
    var timeEl = document.getElementById('seek-time');
    if (fill) fill.style.width = pct + '%';
    if (handle) handle.style.left = pct + '%';
    if (timeEl) timeEl.textContent = _fmtTime(cur);
    _scheduleWaveformRedraw();
}

function _fmtTime(s) {
    var m = Math.floor(s / 60);
    var sec = Math.floor(s % 60);
    return m + ':' + (sec < 10 ? '0' : '') + sec;
}

function _seekFromEvent(e) {
    var bar = document.getElementById('seek-bar');
    if (!bar || !_playerAudio) return;
    var rect = bar.getBoundingClientRect();
    var x = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
    var pct = Math.max(0, Math.min(1, x / rect.width));
    var dur = _playerAudio.duration || 0;
    if (dur && isFinite(dur)) {
        _playerAudio.currentTime = pct * dur;
    }
    var fill = document.getElementById('seek-fill');
    var handle = document.getElementById('seek-handle');
    if (fill) fill.style.width = (pct * 100) + '%';
    if (handle) handle.style.left = (pct * 100) + '%';
}

function _initSeekDrag() {
    var bar = document.getElementById('seek-bar');
    if (!bar) return;
    bar.addEventListener('mousedown', function(e) {
        _seekDragging = true;
        _seekFromEvent(e);
    });
    bar.addEventListener('touchstart', function(e) {
        _seekDragging = true;
        _seekFromEvent(e);
    }, {passive: false});
}

document.addEventListener('mousemove', function(e) {
    if (_seekDragging) _seekFromEvent(e);
});
document.addEventListener('touchmove', function(e) {
    if (_seekDragging) _seekFromEvent(e);
}, {passive: false});
document.addEventListener('mouseup', function() { _seekDragging = false; });
document.addEventListener('touchend', function() { _seekDragging = false; });

// --- Waveform visualization ---
function _loadWaveform(url) {
    if (_waveformTrackUrl === url && _waveformData) {
        _drawWaveform();
        return;
    }
    _waveformData = null;
    _waveformTrackUrl = url;
    // Fetch audio data and compute waveform peaks
    fetch(url, {credentials: 'include'})
        .then(function(r) { return r.arrayBuffer(); })
        .then(function(buf) {
            var actx = new (window.AudioContext || window.webkitAudioContext)();
            return actx.decodeAudioData(buf).then(function(decoded) {
                actx.close();
                return decoded;
            });
        })
        .then(function(audioBuffer) {
            var raw = audioBuffer.getChannelData(0);
            var bars = 200;
            var blockSize = Math.floor(raw.length / bars);
            var peaks = new Float32Array(bars);
            for (var i = 0; i < bars; i++) {
                var sum = 0;
                var start = i * blockSize;
                for (var j = 0; j < blockSize; j++) {
                    sum += Math.abs(raw[start + j]);
                }
                peaks[i] = sum / blockSize;
            }
            // Normalize
            var max = 0;
            for (var k = 0; k < peaks.length; k++) {
                if (peaks[k] > max) max = peaks[k];
            }
            if (max > 0) {
                for (var m = 0; m < peaks.length; m++) peaks[m] /= max;
            }
            _waveformData = peaks;
            _drawWaveform();
        })
        .catch(function() { _waveformData = null; });
}

function _drawWaveform() {
    var canvas = document.getElementById('waveform-canvas');
    if (!canvas || !_waveformData) return;
    var ctx = canvas.getContext('2d');
    var w = canvas.width;
    var h = canvas.height;
    var data = _waveformData;
    var bars = data.length;
    var barW = w / bars;

    ctx.clearRect(0, 0, w, h);

    // Draw waveform bars
    var progress = 0;
    if (_playerAudio && _playerAudio.duration && isFinite(_playerAudio.duration)) {
        progress = _playerAudio.currentTime / _playerAudio.duration;
    }

    for (var i = 0; i < bars; i++) {
        var val = data[i];
        var barH = Math.max(1, val * (h - 2));
        var x = i * barW;
        var pct = i / bars;
        if (pct < progress) {
            ctx.fillStyle = '#4caf50';
        } else {
            ctx.fillStyle = 'rgba(255,255,255,0.15)';
        }
        ctx.fillRect(x, (h - barH) / 2, Math.max(1, barW - 0.5), barH);
    }
}

// Redraw waveform on time update to show progress coloring
var _waveformRedrawTimer = null;
function _scheduleWaveformRedraw() {
    if (_waveformRedrawTimer) return;
    _waveformRedrawTimer = setTimeout(function() {
        _waveformRedrawTimer = null;
        _drawWaveform();
    }, 250);
}

function _refreshPlayerBar() {
    var container = document.getElementById('player-container');
    if (!container) return;

    var hasCast = _playerData && _playerData.active && _playerData.players && _playerData.players.length > 0;
    var hasBrowser = !!_playerStreamId || _isLibraryTrack;
    var totalPlayers = 0;

    var html = '';

    // Render browser player bar first (if listening)
    if (hasBrowser) {
        html += _renderBrowserPlayerHTML();
        totalPlayers++;
    }

    if (hasCast) {
        // Multiroom group count
        _multiroomGroupCount = 0;
        if (_playerData.speakers) {
            _playerData.speakers.forEach(function(s) { if (s.active_for) _multiroomGroupCount++; });
        }

        var players = _playerData.players.slice(0, MAX_CASTS);
        players.forEach(function(p, idx) {
            html += _renderPlayerHTML(p, idx);
        });
        totalPlayers += players.length;

        // Update volume sliders from server data (only if not dragging/stepping)
        // (deferred to after innerHTML set)
    }

    if (!hasCast && !hasBrowser) {
        html = '<div class="player-bar">'
            + '<div class="player-bar-inner">'
            + '<div class="player-cover-wrap"><div class="player-cover-placeholder"><svg width="20" height="20" viewBox="0 0 24 24" fill="#555"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55C7.79 13 6 14.79 6 17s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg></div></div>'
            + '<div class="player-info"><div class="player-track">' + t('player.no_cast') + '</div><div class="player-stream"></div></div>'
            + '</div></div>';
        totalPlayers = 1;
    }

    container.innerHTML = html;

    // Update cast volume sliders from server data
    if (hasCast) {
        _playerData.players.slice(0, MAX_CASTS).forEach(function(p) {
            var vs = _getVolState(p.device_id);
            if (!vs.dragging && _playerVolumeLocal[p.device_id] == null) {
                var serverVol = p.volume != null ? p.volume : 50;
                _setVolSlider(p.device_id, serverVol);
            }
        });
    }

    _updateVersionPosition(totalPlayers);
    _initVolSliders();

    // Init seek drag + visualizer for library tracks
    if (hasBrowser && _isLibraryTrack) {
        _initSeekDrag();
        if (_playerAudio && _playerAudio.src) _loadWaveform(_playerAudio.src);
    } else {
        _waveformData = null;
    }

    if (_multiroomOpen && hasCast) _renderMultiroomPanel(_playerData);
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

var _pausedStreams = {};

function playerPause(streamId, deviceId) {
    fetch('/api/cast/pause', {
        method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({stream_id: parseInt(streamId)}),
    }).then(function() {
        var key = streamId + ':' + deviceId;
        _pausedStreams[key] = !_pausedStreams[key];
        _refreshPlayerBar();
    }).catch(function() {});
}

function playerStop(streamId, deviceId) {
    fetch('/api/cast/stop', {
        method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({device_id: deviceId}),
    }).then(function() {
        // Remove this device from the stream's active list
        var arr = _castActiveCache[streamId] || [];
        var idx = arr.indexOf(deviceId);
        if (idx !== -1) arr.splice(idx, 1);
        if (arr.length === 0) delete _castActiveCache[streamId];
        var key = streamId + ':' + deviceId;
        delete _pausedStreams[key];
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
    if (deviceId === 'browser') {
        setBrowserVolume(_getVolState(deviceId).value / 100);
    } else {
        _volCommit(deviceId, _getVolState(deviceId).value);
    }
    _activeVolDrag = null;
});
document.addEventListener('touchend', function() {
    if (!_activeVolDrag) return;
    var deviceId = _activeVolDrag;
    _getVolState(deviceId).dragging = false;
    _playerVolumeLocal[deviceId] = null;
    if (deviceId === 'browser') {
        setBrowserVolume(_getVolState(deviceId).value / 100);
    } else {
        _volCommit(deviceId, _getVolState(deviceId).value);
    }
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
    // Poll ICY for browser listen
    _pollBrowserIcy();
};

// Poll ICY metadata for browser listen (even when not recording)
function _pollBrowserIcy() {
    if (!_playerStreamId) { if (!_isLibraryTrack) { _browserIcyTrack = ''; _browserIcyCover = null; } return; }
    fetch('/api/stream/' + _playerStreamId + '/icy', {credentials: 'include'})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.current_track) _browserIcyTrack = data.current_track;
            if (data.cover_url) _browserIcyCover = data.cover_url;
            _refreshPlayerBar();
        })
        .catch(function() {});
}

// Restore browser listen state from localStorage (persists across page navigation)
_restoreListenState();

// Initialize browser volume state for slider
_getVolState('browser').value = Math.round(_browserVolume * 100);

// Start polling
setInterval(updateStatus, 5000);
updateStatus();
