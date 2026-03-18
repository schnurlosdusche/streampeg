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
                        statusCell.innerHTML = '<span class="status-skipping">skipping</span>';
                    } else if (s.running && rs === 'recording') {
                        statusCell.innerHTML = '<span class="status-recording">recording</span>';
                    } else if (s.running && rs === 'waiting') {
                        statusCell.innerHTML = '<span class="status-running">running</span>';
                    } else if (s.running) {
                        var ct = (s.current_track || '').trim();
                        var hasTrack = ct && ct !== 'recording' && ct !== '-' && ct.replace(/[\s-]/g, '') !== '';
                        if (hasTrack) {
                            statusCell.innerHTML = '<span class="status-recording">recording</span>';
                        } else {
                            statusCell.innerHTML = '<span class="status-running">running</span>';
                        }
                    } else {
                        statusCell.innerHTML = '<span class="status-stopped">stop</span>';
                    }
                }

                // Stats subtitle under track info
                const track = row.querySelector('.track-cell');
                if (track) {
                    var trackHtml;
                    var rs = s.rec_state || '';
                    var ct = s.current_track || '';
                    var hasTrack = ct && ct !== 'recording' && ct !== '-' && ct.replace(/[\s-]/g, '') !== '';
                    if (rs === 'waiting') { trackHtml = 'waiting for next track'; }
                    else if (hasTrack) { trackHtml = ct; }
                    else if (s.running) { trackHtml = 'waiting for next track'; }
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
                        sub += ' / ' + s.yt_stats.songs_seen + ' gehört';
                        if (s.running && s.bitrate) sub += ' · ' + s.bitrate + ' kbps';
                        trackHtml += '<small style="display:block;color:var(--pico-muted-color,#888);">' + sub + '</small>';
                    } else if (s.track_stats && (s.track_stats.recorded + s.track_stats.skipped) > 0) {
                        var recSub = s.track_stats.recorded + ' rec / ' + (s.track_stats.recorded + s.track_stats.skipped) + ' gehört';
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
                btn.textContent = data.success ? 'OK' : 'Fehler';
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
            btn.textContent = data.started + ' gestartet';
            setTimeout(() => { btn.textContent = 'Start All'; btn.disabled = false; }, 2000);
            updateStatus();
        })
        .catch(() => { btn.textContent = 'Start All'; btn.disabled = false; });
}

function stopAll(btn) {
    if (!confirm('Alle Streams stoppen?')) return;
    btn.disabled = true;
    btn.textContent = '...';
    fetch('/api/stop-all', {method: 'POST', credentials: 'include', headers: {'X-Requested-With': 'XMLHttpRequest'}})
        .then(r => r.json())
        .then(data => {
            btn.textContent = data.stopped + ' gestoppt';
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
    menu.innerHTML = '<div class="cast-menu-loading">Suche Geräte...</div>';
    // Position below button
    var rect = btn.getBoundingClientRect();
    menu.style.position = 'fixed';
    menu.style.left = rect.left + 'px';
    menu.style.top = rect.bottom + 2 + 'px';
    document.body.appendChild(menu);
    _castMenu = menu;

    // Fetch devices
    fetch('/api/cast/devices?refresh=1', {credentials: 'include'})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (!_castMenu) return;
            _castDevicesCache = data.devices || [];
            _castActiveCache = data.active_casts || {};
            _renderCastMenu(menu, streamId);
        })
        .catch(function() {
            if (!_castMenu) return;
            menu.innerHTML = '<div class="cast-menu-empty">Fehler beim Laden</div>';
        });

    // Close on outside click
    setTimeout(function() {
        document.addEventListener('click', _closeCastMenuOutside);
    }, 10);
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
        html = '<div class="cast-menu-empty">Keine Geräte gefunden</div>';
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
            if (!isEnabled) label += '<span class="cast-device-type"> (deaktiviert)</span>';
            if (isActive) label = '&#9654; ' + label;
            if (d.model) label += '<span class="cast-device-type"> ' + d.model + '</span>';

            html += '<div class="cast-menu-device">';
            html += '<button class="' + cls + '" data-device-id="' + d.id + '" data-enabled="' + isEnabled + '" onclick="castToDevice(' + streamId + ', \'' + d.id + '\', ' + isEnabled + ')">&#9654; Abspielen: ' + label + '</button>';
            if (isEnabled) {
                html += '<button class="cast-menu-item cast-menu-queue-add" onclick="addToQueue(' + streamId + ', \'' + d.id + '\')">+ Warteschlange: ' + d.name + '</button>';
            }
            html += '</div>';
        });
    }

    // Stop button if actively casting
    if (activeDeviceId) {
        html += '<button class="cast-menu-item cast-menu-stop" onclick="stopCast(' + streamId + ')">Wiedergabe stoppen</button>';
    }

    menu.innerHTML = html;
}

function castToDevice(streamId, deviceId, enabled) {
    if (!enabled) return;
    if (_castMenu) { _castMenu.remove(); _castMenu = null; }

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
            alert(data.message || 'Cast fehlgeschlagen');
        }
    })
    .catch(function() { alert('Cast fehlgeschlagen'); });
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

// --- Cast Player Bar (external devices only) ---
var _playerData = null;
var _playerVolumeDebounce = null;
var _playerVolumeLocal = null;
var _multiroomOpen = false;
var _lastStreamStatus = {}; // stream_id -> status from /api/status

function _updatePlayerBar() {
    fetch('/api/cast/player', {credentials: 'include'})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            _playerData = data;
            _refreshPlayerBar();
        })
        .catch(function() {});
}

function _refreshPlayerBar() {
    var bar = document.getElementById('player-bar');
    if (!bar) return;

    var cover = document.getElementById('player-cover');
    var placeholder = document.getElementById('player-cover-placeholder');
    var trackEl = document.getElementById('player-track');
    var streamEl = document.getElementById('player-stream');
    var stopBtn = document.getElementById('player-stop-btn');
    var slider = document.getElementById('player-volume-slider');
    var valSpan = document.getElementById('player-volume-value');
    var devName = document.getElementById('player-device-name');
    var mrBtn = document.getElementById('player-multiroom-btn');
    var mrPanel = document.getElementById('player-multiroom');

    var hasCast = _playerData && _playerData.active && _playerData.players && _playerData.players.length > 0;

    // === IDLE — no active cast ===
    if (!hasCast) {
        cover.style.display = 'none';
        placeholder.style.display = '';
        trackEl.textContent = 'Kein Cast aktiv';
        streamEl.textContent = '';
        stopBtn.style.display = 'none';
        devName.textContent = '';
        mrPanel.style.display = 'none';
        slider.value = 0;
        valSpan.textContent = '-';
        slider.removeAttribute('data-device-id');
        return;
    }

    // === CAST active ===
    var p = _playerData.players[0];
    var st = _lastStreamStatus[p.stream_id] || {};
    var castTrack = p.current_track || st.current_track || '';
    var castHasTrack = castTrack && castTrack !== '-' && castTrack !== 'recording' && castTrack.replace(/[\s-]/g, '') !== '';
    var castCover = p.cover_url || st.cover_url || null;

    if (castCover) {
        cover.src = castCover;
        cover.style.display = '';
        placeholder.style.display = 'none';
    } else {
        cover.style.display = 'none';
        placeholder.style.display = '';
    }
    trackEl.textContent = castHasTrack ? castTrack : 'Warte auf Track...';
    streamEl.textContent = p.stream_name;
    stopBtn.style.display = '';
    stopBtn.setAttribute('data-stream-id', p.stream_id);
    devName.textContent = p.device_name;
    mrPanel.style.display = '';

    // Volume
    if (slider && !slider.matches(':active') && _playerVolumeLocal === null) {
        var vol = p.volume !== null && p.volume !== undefined ? p.volume : 50;
        slider.value = vol;
        valSpan.textContent = vol;
    }
    slider.setAttribute('data-device-id', p.device_id);

    // Multiroom button
    var groupCount = 0;
    if (_playerData.speakers) {
        _playerData.speakers.forEach(function(s) { if (s.active_for) groupCount++; });
    }
    mrBtn.classList.toggle('has-group', groupCount > 1);
    if (_multiroomOpen) _renderMultiroomPanel(_playerData);
}

function playerStop() {
    if (!_playerData || !_playerData.players || !_playerData.players.length) return;
    var streamId = _playerData.players[0].stream_id;
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

// Volume slider events (cast only)
document.addEventListener('DOMContentLoaded', function() {
    var slider = document.getElementById('player-volume-slider');
    if (!slider) return;
    slider.addEventListener('input', function() {
        var valSpan = document.getElementById('player-volume-value');
        valSpan.textContent = slider.value;
        _playerVolumeLocal = parseInt(slider.value);
    });
    slider.addEventListener('change', function() {
        var val = parseInt(slider.value);
        var devId = slider.getAttribute('data-device-id');
        _playerVolumeLocal = null;
        if (devId) {
            clearTimeout(_playerVolumeDebounce);
            _playerVolumeDebounce = setTimeout(function() {
                fetch('/api/cast/volume/' + encodeURIComponent(devId), {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({volume: val}),
                }).catch(function() {});
            }, 200);
        }
    });
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
    var btn = document.getElementById('player-multiroom-btn');
    if (panel && !panel.contains(e.target) && !btn.contains(e.target)) {
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
        html += '<span class="multiroom-speaker-name">' + s.name + (isMaster ? ' (Master)' : '') + '</span>';
        html += badge;
        html += '</div>';
    });

    if (!html) html = '<div style="padding:0.5rem 0.8rem;color:#888;">Keine weiteren Geräte</div>';
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
            alert(data.error || 'Fehler');
        }
    })
    .catch(function() { alert('Fehler beim Hinzufuegen zur Warteschlange'); });
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
            alert(data.error || 'Warteschlange ist leer');
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
                    html += '<button class="queue-item-remove" onclick="removeFromQueue(\'' + deviceId + '\', ' + idx + ')" title="Entfernen">&#10005;</button>';
                    html += '</div>';
                });

                html += '<div class="queue-controls">';
                html += '<button class="btn-icon outline queue-btn" onclick="advanceQueue(\'' + deviceId + '\')">Naechster</button>';
                html += '<button class="btn-icon outline queue-btn queue-btn-clear" onclick="clearQueue(\'' + deviceId + '\')">Leeren</button>';

                if (info.timer) {
                    html += '<span class="queue-timer-info">Timer: ' + info.timer.remaining + ' Min verbleibend</span>';
                    html += '<button class="btn-icon outline queue-btn queue-btn-timer-stop" onclick="cancelQueueTimer(\'' + deviceId + '\')">Timer stoppen</button>';
                } else {
                    html += '<input type="number" class="queue-timer-input" data-device-id="' + deviceId + '" value="30" min="1" max="999" title="Minuten">';
                    html += '<button class="btn-icon outline queue-btn" onclick="setQueueTimer(\'' + deviceId + '\')">Timer setzen</button>';
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
