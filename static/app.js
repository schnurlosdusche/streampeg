// --- Tab visibility tracking ---
var _tabVisible = !document.hidden;
document.addEventListener('visibilitychange', function() {
    _tabVisible = !document.hidden;
    if (_tabVisible) {
        // Notify server that client is back
        fetch('/api/heartbeat', {method: 'POST', credentials: 'include'}).catch(function(){});
    }
});

// Stop cast when browser/tab closes
window.addEventListener('beforeunload', function() {
    if (_libCastDeviceIds && _libCastDeviceIds.length > 0) {
        _libCastDeviceIds.forEach(function(did) {
            navigator.sendBeacon('/api/cast/stop', JSON.stringify({device_id: did}));
        });
        sessionStorage.removeItem('_libCastDeviceIds');
        sessionStorage.removeItem('_castPlayStart');
    }
});

// Poll status every 5 seconds
function updateStatus() {
    var _isPlaying = _playerAudio && !_playerAudio.paused && !_playerAudio.ended ? '1' : '0';
    fetch('/api/status', {credentials: 'include', headers: {'X-Tab-Visible': _tabVisible ? '1' : '0', 'X-Audio-Playing': _isPlaying}})
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
                    bitrate: s.bitrate || 0,
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

// --- Camelot wheel mapping for player ---
var _CAMELOT_PLAYER = {"Abm":"1A","G#m":"1A","Ebm":"2A","D#m":"2A","Bbm":"3A","A#m":"3A","Fm":"4A","Cm":"5A","Gm":"6A","Dm":"7A","Am":"8A","Em":"9A","Bm":"10A","F#m":"11A","Gbm":"11A","C#m":"12A","Dbm":"12A","B":"1B","Cb":"1B","F#":"2B","Gb":"2B","C#":"3B","Db":"3B","Ab":"4B","G#":"4B","Eb":"5B","D#":"5B","Bb":"6B","A#":"6B","F":"7B","C":"8B","G":"9B","D":"10B","A":"11B","E":"12B"};

// --- Browser listen (persistent across page navigation via localStorage) ---
var _playerAudio = null;
var _playerStreamId = null;
var _playerStreamUrl = null;
var _browserVolume = parseFloat(localStorage.getItem('_browserVolume') || '0.32');
var _browserPaused = false;
var _browserIcyTrack = '';
var _browserIcyCover = null;
var _isLibraryTrack = false;
var _browserLibStream = sessionStorage.getItem('_browserLibStream') || '';
var _libCastDeviceId = null;  // primary cast device for library player (volume control)
var _libCastDeviceName = '';  // display name of primary cast device
var _libCastDeviceIds = [];   // all active cast device IDs
var _castPlayStart = 0;       // timestamp when cast started current track
var _castTrackDuration = 0;   // duration of currently casting track (seconds)
var _libTrackDuration = 0;    // DB duration for current library track (fallback for broken headers)
var _seekDragging = false;
var _lastSeekTime = 0;          // timestamp of last seek action (to suppress premature ended)
var _loopMode = false; // arrow key micro-loop active
var _loopStart = 0;
var _loopLength = 2; // 2 seconds default loop length
var _seekStep = 5; // seconds to jump with left/right arrows
var _waveformData = null; // Float32Array of peak values for current track
var _waveformTrackUrl = null;
var _libRepeatMode = localStorage.getItem('_libRepeatMode') || 'none'; // none, all, one
var _libShuffleMode = localStorage.getItem('_libShuffleMode') === '1'; // true/false
var _cuePoints = {}; // {trackId: {1: timeInSec, 2: timeInSec, ...}}
var _CUE_COLORS = ['#42a5f5','#ff9800','#4caf50','#e91e63','#9c27b0','#00bcd4','#ffeb3b','#ff5722'];
var _trackRatings = {}; // {trackId: 0-5}
var _trackUnusable = {}; // {trackId: 0|1}

function _isTrackUnusable(trackOrId) {
    if (localStorage.getItem('_skipUnusable') !== '1') return false;
    if (typeof trackOrId === 'object') return !!trackOrId.unusable;
    return !!(_trackUnusable[trackOrId] || (typeof _libTrackCache !== 'undefined' && _libTrackCache[trackOrId] && _libTrackCache[trackOrId].unusable));
}

function _onPlayerError() {
    // Don't stop if AutoDJ just swapped the audio
    if (typeof AutoDJ !== 'undefined' && AutoDJ._crossfadeComplete) return;
    // Ignore errors shortly after seeking (browser may fail to decode at seek point)
    if (_lastSeekTime && (Date.now() - _lastSeekTime) < 3000) {
        console.warn('Player error suppressed after seek, retrying playback');
        _playerAudio.play().catch(function() {});
        return;
    }
    stopListen();
}

function _initBrowserAudio() {
    if (!_playerAudio) {
        _playerAudio = new Audio();
        _playerAudio.crossOrigin = 'anonymous';
        _playerAudio.volume = _browserVolume;
        _playerAudio.addEventListener('error', _onPlayerError);
        _playerAudio.addEventListener('timeupdate', _onSeekUpdate);
        _playerAudio.addEventListener('ended', _onPlayerEnded);
        _playerAudio.addEventListener('loadedmetadata', function() {
            if (_playerStreamId === 'browse') _refreshPlayerBar();
        });
        _playerAudio.addEventListener('durationchange', function() {
            if (_playerStreamId === 'browse' && _playerAudio.duration && isFinite(_playerAudio.duration)) _refreshPlayerBar();
        });
    }
}

function _onPlayerEnded() {
            // If Auto-DJ crossfade just completed, ignore all ended events
            if (typeof AutoDJ !== 'undefined' && AutoDJ._crossfadeComplete) {
                AutoDJ._log('ENDED: ignored (crossfadeComplete, early check)');
                return;
            }
            // Guard against premature 'ended' from defective MP3s or seek artifacts
            // Skip guard if repeat-one is active (always honor ended)
            if (_isLibraryTrack && _libRepeatMode !== 'one') {
                // Block ended events for 2s after seeking (browsers fire false ended on seek)
                if (_lastSeekTime && (Date.now() - _lastSeekTime) < 2000) {
                    console.warn('Ended event suppressed: too close to seek (' + (Date.now() - _lastSeekTime) + 'ms)');
                    _playerAudio.play().catch(function() {});
                    return;
                }
                var _edDur = (_playerAudio.duration && isFinite(_playerAudio.duration)) ? _playerAudio.duration : _libTrackDuration;
                if (_edDur > 0) {
                    var remaining = _edDur - _playerAudio.currentTime;
                    if (remaining > 5) {
                        if (typeof AutoDJ !== 'undefined' && AutoDJ._debug) AutoDJ._log('ENDED BLOCKED by premature guard: remaining=' + remaining.toFixed(1) + ' cur=' + _playerAudio.currentTime.toFixed(1) + ' dur=' + _edDur.toFixed(1));
                        console.warn('Premature ended event at', _playerAudio.currentTime, '/', _playerAudio.duration);
                        _playerAudio.play().catch(function() {});
                        return;
                    }
                }
            }
            if (_isLibraryTrack && _libRepeatMode === 'one') {
                // Repeat current track
                _playerAudio.currentTime = 0;
                _playerAudio.play().catch(function() {});
                return;
            }
            if (_isLibraryTrack && typeof AutoDJ !== 'undefined' && AutoDJ.enabled) {
                AutoDJ._log('ENDED event: _fading=' + AutoDJ._fading + ' _fadeAudio=' + (AutoDJ._fadeAudio ? 'yes' : 'no') + ' _crossfadeComplete=' + AutoDJ._crossfadeComplete + ' _playerAudio.paused=' + (_playerAudio ? _playerAudio.paused : '?'));
                // Crossfade in progress — finalize it
                if (AutoDJ._fading || AutoDJ._fadeAudio) {
                    AutoDJ._log('ENDED: calling _finishCrossfade');
                    AutoDJ._finishCrossfade();
                    return;
                }
                // No crossfade happened — just pick next
                AutoDJ._log('ENDED: no crossfade, calling playNext');
                AutoDJ.playNext();
                return;
            }
            if (_isLibraryTrack && _libShuffleMode) {
                // Shuffle: play random next
                _playRandomTrack();
                return;
            }
            if (_isLibraryTrack && _libRepeatMode === 'all' && typeof _libPlayNextTrack === 'function') {
                // Play next track in list, wrap around
                _libPlayNextTrack(true);
                return;
            }
            // No repeat or stream ended: reset everything
            var wasLibrary = _isLibraryTrack;
            _isLibraryTrack = false;
            if (typeof _libPlayingTrackId !== 'undefined') _libPlayingTrackId = null;
            stopListen();
            if (wasLibrary && typeof updatePlayButtons === 'function') updatePlayButtons();
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
    // Stop all cast devices if library track was casting
    if (_libCastDeviceIds && _libCastDeviceIds.length > 0) {
        _libCastDeviceIds.forEach(function(did) {
            fetch('/api/cast/stop', {
                method: 'POST', credentials: 'include',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({device_id: did})
            }).catch(function() {});
        });
    }
    _libCastDeviceId = null;
    _libCastDeviceName = '';
    _libCastDeviceIds = [];
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

var _browseStreamName = '';

function playBrowseStream(url, stationName) {
    _initBrowserAudio();
    // Stop current playback
    if (_playerStreamId || _isLibraryTrack) {
        _playerAudio.pause();
        _playerAudio.removeAttribute('src');
        _playerAudio.load();
    }
    _isLibraryTrack = false;
    _waveformData = null;
    _playerStreamId = 'browse';
    // Local API URLs can be played directly, external URLs need the listen proxy
    _playerStreamUrl = url.startsWith('/api/') ? url : '/api/listen?url=' + encodeURIComponent(url);
    _browseStreamName = stationName || url;
    _playerAudio.volume = _browserVolume;
    _playerAudio.src = _playerStreamUrl;
    _playerAudio.play().catch(function() {});
    localStorage.setItem('_listenStreamId', 'browse');
    localStorage.setItem('_listenStreamUrl', _playerStreamUrl);
    localStorage.setItem('_browseStreamName', _browseStreamName);
    _refreshPlayerBar();
}

function stopBrowseStream() {
    if (_playerStreamId === 'browse') {
        stopListen();
        localStorage.removeItem('_browseStreamName');
    }
}

function _restoreListenState() {
    var savedId = localStorage.getItem('_listenStreamId');
    var savedUrl = localStorage.getItem('_listenStreamUrl');
    if (savedId && savedUrl) {
        _initBrowserAudio();
        if (savedId === 'browse') {
            _playerStreamId = 'browse';
            _browseStreamName = localStorage.getItem('_browseStreamName') || '';
        } else if (savedId.indexOf('bm-') === 0) {
            _playerStreamId = savedId;
        } else {
            _playerStreamId = parseInt(savedId);
        }
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
    if (!_playerAudio) return;
    if (!_playerStreamId && !_isLibraryTrack) return;
    if (_browserPaused) {
        if (_isLibraryTrack) {
            _playerAudio.play().catch(function() {});
        } else {
            _playerAudio.src = _playerStreamUrl;
            _playerAudio.play().catch(function() {});
        }
        _browserPaused = false;
    } else {
        _playerAudio.pause();
        if (!_isLibraryTrack) {
            _playerAudio.removeAttribute('src');
            _playerAudio.load();
        }
        _browserPaused = true;
    }
    _refreshPlayerBar();
}

function _updateListenIcons(activeId) {
    document.querySelectorAll('.btn-listen').forEach(function(el) {
        var sid = el.getAttribute('data-stream-id');
        if (sid) sid = parseInt(sid);
        // Also check bookmark buttons (data-bookmark-id on parent tr)
        if (!sid) {
            var tr = el.closest('tr[data-bookmark-id]');
            if (tr) sid = 'bm-' + tr.dataset.bookmarkId;
        }
        if (sid == activeId) {
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
        + _renderCastHeartBtn(p.stream_id, castTrack)
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

function _renderLibCastPlayerHTML(p) {
    var castTrack = p.current_track || '';
    var castHasTrack = castTrack && castTrack.replace(/[\s\-]/g, '') !== '';
    var castCover = p.cover_url || null;
    var vol = _getVolState(p.device_id);
    var curVol = (_playerVolumeLocal[p.device_id] != null && _playerVolumeLocal[p.device_id] !== true)
        ? _playerVolumeLocal[p.device_id] : (p.volume != null ? p.volume : 50);

    var coverHtml = castCover
        ? '<img src="' + castCover + '" alt="">'
        : '<div class="player-cover-placeholder"><svg width="20" height="20" viewBox="0 0 24 24" fill="#555"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55C7.79 13 6 14.79 6 17s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg></div>';

    var html = '<div class="player-bar player-bar-libcast" data-device-id="' + p.device_id + '">'
        + '<div class="player-bar-inner">'
        + '<div class="player-cover-wrap">' + coverHtml + '</div>'
        + '<div class="player-info">'
        + '<div class="player-track">' + (castHasTrack ? _escHtmlPlayer(castTrack) : t('player.waiting_track')) + '</div>'
        + '<div class="player-stream">Library</div>'
        + '</div>'
        + '<div class="player-volume">'
        // Prev
        + '<button class="player-btn" onclick="_libPlayPrevTrack()" title="Previous">'
        + '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="2" y="4" width="3" height="16" rx="1"/><path d="M22 4L9 12l13 8z"/></svg>'
        + '</button>'
        // Stop
        + '<button class="player-btn" onclick="stopLibCast(\'' + p.device_id + '\')" title="Stop">'
        + '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>'
        + '</button>'
        // Next
        + '<button class="player-btn" onclick="_libPlayNextTrack(false)" title="Next">'
        + '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="19" y="4" width="3" height="16" rx="1"/><path d="M2 4l13 8-13 8z"/></svg>'
        + '</button>'
        // Volume
        + '<button class="player-vol-btn" style="margin-left:0.5rem;" onclick="playerVolumeStep(\'' + p.device_id + '\', -2)">&#8722;</button>'
        + '<div class="vol-slider" data-device-id="' + p.device_id + '">'
        + '<div class="vol-slider-track"></div>'
        + '<div class="vol-slider-fill" style="width:' + curVol + '%"></div>'
        + '<div class="vol-slider-handle" style="left:' + curVol + '%"></div>'
        + '</div>'
        + '<button class="player-vol-btn" onclick="playerVolumeStep(\'' + p.device_id + '\', 2)">+</button>'
        + '<span class="player-volume-value">' + curVol + '</span>'
        + '</div>'
        + '<div class="player-right">'
        + '<div class="player-device-name">' + _escHtmlPlayer(p.device_name) + '</div>'
        + '</div>'
        + '</div></div>';
    return html;
}

function stopLibCast(deviceId) {
    fetch('/api/cast/stop', {
        method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({device_id: deviceId})
    }).catch(function() {});
    // Remove from active list
    if (_libCastDeviceIds) {
        _libCastDeviceIds = _libCastDeviceIds.filter(function(id) { return id !== deviceId; });
    }
    if (_libCastDeviceId === deviceId) {
        if (_libCastDeviceIds.length > 0) {
            _libCastDeviceId = _libCastDeviceIds[_libCastDeviceIds.length - 1];
        } else {
            _libCastDeviceId = null;
            _libCastDeviceName = '';
            _stopCastPoll();
        }
    }
    _refreshPlayerBar();
}

var _multiroomGroupCount = 0;

function _renderBrowserPlayerHTML() {
    var st = _lastStreamStatus[_playerStreamId] || {};
    // Use ICY data if available (fetched independently), fall back to recording status
    var _isCasting = _libCastDeviceIds && _libCastDeviceIds.length > 0;
    var trackName = _browserIcyTrack || st.current_track || '';
    var hasTrack = trackName && trackName !== 'recording' && trackName !== '-' && trackName.replace(/[\s\-]/g, '') !== '';
    var coverUrl = _browserIcyCover || st.cover_url || null;
    // For library cast: get track name and cover from cache
    if (_isLibraryTrack && _isCasting && _libPlayingTrackId) {
        var _ct = (typeof _libTrackCache !== 'undefined') ? _libTrackCache[_libPlayingTrackId] : null;
        if (_ct) {
            var _a = _ct.artist || '', _t = _ct.title || '';
            trackName = _a ? (_a + ' - ' + _t) : _t;
            hasTrack = !!trackName;
        }
        // Use cover from _loadLibraryCover (embedded or iTunes), fallback to embedded endpoint
        if (!coverUrl) coverUrl = '/api/library/track/' + _libPlayingTrackId + '/cover';
    }
    var streamName = (_playerStreamId === 'browse') ? _browseStreamName : (st.stream_name || ('Stream ' + _playerStreamId));
    var volPct = Math.round(_browserVolume * 100);

    var coverHtml = coverUrl
        ? '<img src="' + coverUrl + '" alt="">'
        : '<div class="player-cover-placeholder"><svg width="20" height="20" viewBox="0 0 24 24" fill="#555"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55C7.79 13 6 14.79 6 17s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg></div>';

    // Seek bar for tracks with finite duration (library tracks + browse streams like YT preview)
    var seekHtml = '';
    var cueHtml = '';
    var _browseHasDuration = !_isLibraryTrack && _playerStreamId === 'browse' && _playerAudio && _playerAudio.duration && isFinite(_playerAudio.duration) && _playerAudio.duration > 0;
    if ((_isLibraryTrack || _browseHasDuration) && (_playerAudio || _isCasting)) {
        var dur, cur, seekPct;
        if (_isCasting && _castPlayStart && _castTrackDuration > 0) {
            dur = _castTrackDuration;
            cur = (Date.now() / 1000) - _castPlayStart;
            if (cur > dur) cur = dur;
            seekPct = (cur / dur * 100);
        } else if (_playerAudio) {
            dur = (_playerAudio.duration && isFinite(_playerAudio.duration)) ? _playerAudio.duration : _libTrackDuration;
            cur = _playerAudio.currentTime || 0;
            seekPct = (dur > 0) ? (cur / dur * 100) : 0;
        } else {
            dur = 0; cur = 0; seekPct = 0;
        }

        // Cue point markers inside the waveform
        var cueMarkers = '';
        var trackCues = _cuePoints[_libPlayingTrackId] || {};
        if (dur && isFinite(dur)) {
            for (var cn = 1; cn <= 8; cn++) {
                if (trackCues[cn] != null) {
                    var cuePct = (trackCues[cn] / dur * 100);
                    cueMarkers += '<div class="cue-marker" data-cue="' + cn + '" style="left:' + cuePct + '%;background:' + _CUE_COLORS[cn-1] + ';"></div>';
                }
            }
        }

        var noSeek = '';
        seekHtml = '<div class="player-seek-row">'
            + '<span class="player-seek-time" id="seek-time">' + _fmtTime(cur) + '</span>'
            + '<div class="player-seek-bar" id="seek-bar"' + noSeek + '>'
            + '<canvas id="waveform-canvas" class="player-waveform" width="1200" height="80"></canvas>'
            + '<div class="player-seek-overlay" id="seek-fill" style="width:' + seekPct + '%"></div>'
            + '<div class="player-seek-handle" id="seek-handle" style="left:' + seekPct + '%"><span class="seek-remaining" id="seek-remaining"></span></div>'
            + cueMarkers
            + '</div>'
            + '<span class="player-seek-time" id="seek-dur">' + _fmtTime(dur) + '</span>'
            + '</div>';

        // Cue buttons row — only for library tracks in browser mode (not for browse/cast)
        if (_isCasting || _browseHasDuration) { cueHtml = ''; } else {
        cueHtml = '<div class="player-cue-row"><div class="player-cue-buttons">';
        for (var ci = 1; ci <= 8; ci++) {
            var isSet = trackCues[ci] != null;
            var cueColor = _CUE_COLORS[ci-1];
            cueHtml += '<button class="cue-btn' + (isSet ? ' set' : '') + '" '
                + 'style="--cue-color:' + cueColor + ';" '
                + 'onclick="cueAction(' + ci + ')" '
                + 'oncontextmenu="cueClear(' + ci + ',event)" '
                + 'title="' + (isSet ? 'Cue ' + ci + ': ' + _fmtTime(trackCues[ci]) + ' (right-click to clear)' : 'Set cue ' + ci + ' at current position') + '">'
                + ci + '</button>';
        }
        cueHtml += '</div></div>';
        }
    }

    var html = '<div class="player-bar player-bar-browser' + (_isLibraryTrack ? ' player-bar-library' : '') + '">';

    if (_isLibraryTrack) {
        // BPM/Key overlay on cover
        var _bpmKeyOverlay = '';
        var _tid = (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : null;
        var _tdata = (_tid && typeof _libTrackCache !== 'undefined') ? _libTrackCache[_tid] : null;
        if (_tdata) {
            var parts = [];
            if (_tdata.bpm && _tdata.bpm > 0) parts.push(_tdata.bpm + ' BPM');
            if (_tdata.key) {
                var _cam = _CAMELOT_PLAYER[_tdata.key] || '';
                parts.push(_tdata.key + (_cam ? ' / ' + _cam : ''));
            }
            if (parts.length) _bpmKeyOverlay = '<div class="cover-bpm-key">' + parts.join(' &middot; ') + '</div>';
        }
        // Radar animation overlay when playing
        var _radarOverlay = '';
        if (!_browserPaused) {
            _radarOverlay = '<div class="cover-radar">'
                + '<svg viewBox="0 0 100 100" width="60" height="60">'
                + '<line x1="50" y1="50" x2="50" y2="2" stroke="rgba(255,255,255,0.8)" stroke-width="4" stroke-linecap="round"/>'
                + '</svg></div>';
        }
        // Grid layout: cover left spanning rows, top = stream + track + controls + volume
        html += '<div class="player-cover-wrap">' + coverHtml + _bpmKeyOverlay + _radarOverlay + '</div>'
            + '<div class="player-lib-top">'
            + '<div class="player-info">'
            + (_browserLibStream ? '<div class="player-stream">' + _escHtmlPlayer(_browserLibStream) + '</div>' : '')
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

    // Prev button
    html += '<button class="player-btn" onclick="_libPlayPrevTrack()" title="Previous">'
        + '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 6h2v12H6zm3.5 6l8.5 6V6z"/></svg>'
        + '</button>';

    if (_browserPaused) {
        html += '<button class="player-btn" onclick="toggleBrowserPause()" title="Play">'
            + '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>'
            + '</button>';
    } else {
        html += '<button class="player-btn" onclick="toggleBrowserPause()" title="' + t('player.pause_title') + '">'
            + '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="3" width="4" height="18" rx="1"/><rect x="15" y="3" width="4" height="18" rx="1"/></svg>'
            + '</button>';
    }

    html += '<button class="player-btn" onclick="' + (_isCasting ? 'stopAllLibCasts()' : 'stopListen()') + '" title="' + t('player.stop_title') + '">'
        + '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>'
        + '</button>'
        // Fwd button
        + '<button class="player-btn" onclick="_libPlayNextTrack(false)" title="Next">'
        + '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z"/></svg>'
        + '</button>'
        + (function() {
            if (_isCasting && _libCastDeviceId) {
                var castVol = (_playerVolumeLocal[_libCastDeviceId] != null && _playerVolumeLocal[_libCastDeviceId] !== true)
                    ? _playerVolumeLocal[_libCastDeviceId]
                    : ((_volState[_libCastDeviceId] && _volState[_libCastDeviceId].value != null) ? _volState[_libCastDeviceId].value : 50);
                return '<button class="player-vol-btn" style="margin-left:0.5rem;" onclick="playerVolumeStep(\'' + _libCastDeviceId + '\', -2)">&#8722;</button>'
                    + '<div class="vol-slider" data-device-id="' + _libCastDeviceId + '">'
                    + '<div class="vol-slider-track"></div>'
                    + '<div class="vol-slider-fill" style="width:' + castVol + '%"></div>'
                    + '<div class="vol-slider-handle" style="left:' + castVol + '%"></div>'
                    + '</div>'
                    + '<button class="player-vol-btn" onclick="playerVolumeStep(\'' + _libCastDeviceId + '\', 2)">+</button>'
                    + '<span class="player-volume-value">' + castVol + '</span>';
            }
            return '<button class="player-vol-btn" style="margin-left:0.5rem;" onclick="_browserVolumeStep(-5)">&#8722;</button>'
                + '<div class="vol-slider" data-device-id="browser">'
                + '<div class="vol-slider-track"></div>'
                + '<div class="vol-slider-fill" style="width:' + volPct + '%"></div>'
                + '<div class="vol-slider-handle" style="left:' + volPct + '%"></div>'
                + '</div>'
                + '<button class="player-vol-btn" onclick="_browserVolumeStep(5)">+</button>'
                + '<span class="player-volume-value">' + (_isLibraryTrack ? _seekStep + 's' : volPct) + '</span>';
        })()

        + _renderRepeatButton()
        + (_isLibraryTrack ? _renderHeartBtn() + _renderStarRating() + _renderPlayerPlaylistBtn() + _renderPlayerCastBtn() + _renderPlayerTrashBtn()
            : _renderStreamHeartBtn())
        + '</div>'
        + '<div class="player-right">'
        + _renderPlayerBitrate()
        + '<div class="player-device-name">' + (_isCasting ? _getLibCastNames() : t('player.browser')) + '</div>'
        + '</div>';

    if (_isLibraryTrack) {
        // Close top row, add cue buttons then seek bar as grid rows
        html += '</div>' + cueHtml + seekHtml + '</div>';
    } else {
        html += '</div></div>';
    }
    return html;
}

function _renderPlayerBitrate() {
    var br = 0;
    var _brtid = (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : null;
    var _brdata = (_brtid && typeof _libTrackCache !== 'undefined') ? _libTrackCache[_brtid] : null;
    if (_brdata && _brdata.bitrate && _brdata.bitrate > 0) {
        br = _brdata.bitrate;
    } else if (_brdata && _brdata.size_bytes && _brdata.duration_sec && _brdata.duration_sec > 0) {
        br = Math.round((_brdata.size_bytes * 8) / (_brdata.duration_sec * 1000));
    } else if (_playerStreamId && _lastStreamStatus[_playerStreamId]) {
        br = _lastStreamStatus[_playerStreamId].bitrate || 0;
    }
    if (!br) return '';
    var brColor = br >= 128 ? '#4caf50' : '#ff9800';
    return '<div class="player-bitrate" style="color:' + brColor + ';border-color:' + brColor + ';">' + br + ' kBit/s</div>';
}

function _renderRepeatButton() {
    var icon, cls, title;
    if (_libRepeatMode === 'one') {
        // Repeat one: just "1"
        icon = '<span style="font-size:calc(28px * 0.7);line-height:28px;font-weight:200;color:#64b5f6;">1</span>';
        cls = ' repeat-active';
        title = 'Repeat: One';
    } else if (_libRepeatMode === 'all') {
        // Repeat all: filled icon
        icon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z"/></svg>';
        cls = ' repeat-active';
        title = 'Repeat: All';
    } else {
        // No repeat: outline icon
        icon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" opacity="0.4"><path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z"/></svg>';
        cls = '';
        title = 'Repeat: Off';
    }
    return '<button class="player-btn repeat-btn' + cls + '" onclick="toggleRepeatMode()" title="' + title + '" style="margin-left:20px;">' + icon + '</button>'
        + '<button class="player-btn shuffle-btn' + (_libShuffleMode ? ' repeat-active' : '') + '" onclick="toggleShuffleMode()" title="Shuffle: ' + (_libShuffleMode ? 'On' : 'Off') + '" style="margin-left:8px;"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"' + (_libShuffleMode ? '' : ' opacity="0.4"') + '><path d="M10.59 9.17L5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41l-1.41 1.41 3.13 3.13L14.5 20H20v-5.5l-2.04 2.04-3.13-3.13z"/></svg></button>'
        ;
}

function toggleShuffleMode() {
    _libShuffleMode = !_libShuffleMode;
    localStorage.setItem('_libShuffleMode', _libShuffleMode ? '1' : '0');
    // Update button directly without full re-render
    var btn = document.querySelector('.shuffle-btn');
    if (btn) {
        btn.style.color = _libShuffleMode ? '#ff9800' : '';
        btn.style.borderColor = _libShuffleMode ? '#ff9800' : '';
        btn.title = 'Shuffle: ' + (_libShuffleMode ? 'On' : 'Off');
        var svg = btn.querySelector('svg');
        if (svg) svg.setAttribute('opacity', _libShuffleMode ? '1' : '0.4');
    }
}

function _playRandomTrack() {
    // Get current folder's tracks from cache or fetch random from API
    if (typeof _libTracks !== 'undefined' && _libTracks && _libTracks.length > 0) {
        // Filter out unusable tracks if setting enabled
        var candidates = _libTracks.filter(function(t) { return !_isTrackUnusable(t); });
        if (!candidates.length) candidates = _libTracks; // fallback if all unusable
        var idx = Math.floor(Math.random() * candidates.length);
        // Avoid playing the same track
        if (candidates.length > 1) {
            while (candidates[idx].id === _libPlayingTrackId) {
                idx = Math.floor(Math.random() * candidates.length);
            }
        }
        var tr = candidates[idx];
        if (typeof playLibraryTrackById === 'function') {
            playLibraryTrackById(tr.id);
        }
    } else {
        fetch('/api/library/random', {credentials: 'include'})
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.id && typeof playLibraryTrackById === 'function') {
                    playLibraryTrackById(data.id);
                }
            }).catch(function() {});
    }
}

function toggleRepeatMode() {
    if (_libRepeatMode === 'none') _libRepeatMode = 'all';
    else if (_libRepeatMode === 'all') _libRepeatMode = 'one';
    else _libRepeatMode = 'none';
    localStorage.setItem('_libRepeatMode', _libRepeatMode);
    _refreshPlayerBar();
}

// Play previous track in library table
function _libPlayPrevTrack() {
    if (!_libTracks || !_libTracks.length) return;
    var currentIdx = -1;
    for (var i = 0; i < _libTracks.length; i++) {
        if (_libTracks[i].id === _libPlayingTrackId) { currentIdx = i; break; }
    }
    var prevIdx = currentIdx - 1;
    var checked = 0;
    while (checked < _libTracks.length) {
        if (prevIdx < 0) prevIdx = _libTracks.length - 1;
        if (prevIdx === currentIdx) return;
        if (!_isTrackUnusable(_libTracks[prevIdx])) break;
        prevIdx--;
        checked++;
    }
    if (checked >= _libTracks.length) return;
    if (typeof playLibraryTrackById === 'function') playLibraryTrackById(_libTracks[prevIdx].id);
}

// Play next track in library table (for repeat-all / shuffle)
function _libPlayNextTrack(wrap) {
    if (_libShuffleMode) {
        _playRandomTrack();
        return;
    }
    if (!_libTracks || !_libTracks.length) return;
    var currentIdx = -1;
    for (var i = 0; i < _libTracks.length; i++) {
        if (_libTracks[i].id === _libPlayingTrackId) { currentIdx = i; break; }
    }
    // Skip unusable tracks
    var checked = 0;
    var nextIdx = currentIdx + 1;
    while (checked < _libTracks.length) {
        if (nextIdx >= _libTracks.length) {
            if (wrap) nextIdx = 0;
            else return;
        }
        if (nextIdx === currentIdx) return; // wrapped full circle
        if (!_isTrackUnusable(_libTracks[nextIdx])) break;
        nextIdx++;
        checked++;
    }
    if (checked >= _libTracks.length) return; // all unusable
    if (typeof playLibraryTrackById === 'function') playLibraryTrackById(_libTracks[nextIdx].id);
}

// --- Cue Points ---
function cueAction(num) {
    if (!_playerAudio || !_isLibraryTrack || typeof _libPlayingTrackId === 'undefined' || !_libPlayingTrackId) return;
    var trackId = _libPlayingTrackId;
    if (!_cuePoints[trackId]) _cuePoints[trackId] = {};

    if (_cuePoints[trackId][num] != null) {
        // Cue exists: jump to it
        _playerAudio.currentTime = _cuePoints[trackId][num];
        if (_browserPaused) { _playerAudio.play(); _browserPaused = false; }
    } else {
        // Set cue at current position
        _cuePoints[trackId][num] = _playerAudio.currentTime;
        _saveCuePoints(trackId);
    }
    _refreshPlayerBar();
}

function cueInsert() {
    // Insert a new cue at current position, renumber all chronologically
    if (!_playerAudio || !_isLibraryTrack || typeof _libPlayingTrackId === 'undefined' || !_libPlayingTrackId) return;
    var trackId = _libPlayingTrackId;
    if (!_cuePoints[trackId]) _cuePoints[trackId] = {};

    // Collect existing cue positions + new one
    var positions = [];
    for (var n in _cuePoints[trackId]) {
        if (_cuePoints[trackId][n] != null) positions.push(_cuePoints[trackId][n]);
    }
    var newPos = _playerAudio.currentTime;
    // Don't add duplicate (within 0.5s tolerance)
    var isDup = positions.some(function(p) { return Math.abs(p - newPos) < 0.5; });
    if (isDup) return;
    if (positions.length >= 8) return; // max 8 cues

    positions.push(newPos);
    positions.sort(function(a, b) { return a - b; });

    // Reassign numbers 1-N chronologically
    _cuePoints[trackId] = {};
    for (var i = 0; i < positions.length; i++) {
        _cuePoints[trackId][i + 1] = positions[i];
    }
    _saveCuePoints(trackId);
    _refreshPlayerBar();
}

function cueClear(num, e) {
    e.preventDefault(); // prevent context menu
    if (typeof _libPlayingTrackId === 'undefined' || !_libPlayingTrackId) return;
    var trackId = _libPlayingTrackId;
    if (_cuePoints[trackId]) {
        delete _cuePoints[trackId][num];
        // Renumber remaining cues chronologically
        var positions = [];
        for (var n in _cuePoints[trackId]) {
            if (_cuePoints[trackId][n] != null) positions.push(_cuePoints[trackId][n]);
        }
        positions.sort(function(a, b) { return a - b; });
        _cuePoints[trackId] = {};
        for (var i = 0; i < positions.length; i++) {
            _cuePoints[trackId][i + 1] = positions[i];
        }
        _saveCuePoints(trackId);
    }
    _refreshPlayerBar();
}

function _saveCuePoints(trackId) {
    var cues = _cuePoints[trackId] || {};
    fetch('/api/library/track/' + trackId + '/cues', {
        method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cues: cues}),
    }).catch(function() {});
    // Update cue indicator in track list
    var nums = Object.keys(cues).filter(function(k) { return cues[k] != null; }).join(',');
    if (typeof _libTrackCache !== 'undefined' && _libTrackCache[trackId]) {
        _libTrackCache[trackId].cue_nums = nums || null;
    }
    if (typeof _libTracks !== 'undefined') {
        for (var i = 0; i < _libTracks.length; i++) {
            if (_libTracks[i].id === trackId) { _libTracks[i].cue_nums = nums || null; break; }
        }
    }
    if (typeof _vsRenderedStart !== 'undefined') { _vsRenderedStart = -1; _vsRenderVisible(); }
}

function _loadCuePoints(trackId) {
    fetch('/api/library/track/' + trackId + '/cues', {credentials: 'include'})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.cues) {
                _cuePoints[trackId] = {};
                for (var k in data.cues) {
                    _cuePoints[trackId][parseInt(k)] = parseFloat(data.cues[k]);
                }
                _refreshPlayerBar();
            }
        })
        .catch(function() {});
}

// --- Star Rating ---
var _trackFavorites = {}; // {trackId: 0|1}

function _renderHeartBtn() {
    var trackId = (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : null;
    if (!trackId) return '';
    var fav = _trackFavorites[trackId] || 0;
    return '<span class="player-heart' + (fav ? ' heart-active' : '') + '" onclick="toggleFavorite()" title="Favorite" style="margin-left:12px;cursor:pointer;font-size:18px;user-select:none;">' + (fav ? '&#10084;' : '&#9825;') + '</span>';
}

function toggleFavorite() {
    var trackId = (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : null;
    if (!trackId) return;
    fetch('/api/library/track/' + trackId + '/favorite', {
        method: 'POST', credentials: 'include'
    }).then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.success) {
            _trackFavorites[trackId] = data.favorited;
            if (typeof _libTrackCache !== 'undefined' && _libTrackCache[trackId]) {
                _libTrackCache[trackId].favorited = data.favorited;
            }
            // Update player heart directly
            var playerHeart = document.querySelector('.player-heart');
            if (playerHeart) {
                playerHeart.innerHTML = data.favorited ? '&#10084;' : '&#9825;';
                playerHeart.classList.toggle('heart-active', !!data.favorited);
            }
            // Update list heart if visible
            var row = document.querySelector('#lib-table tr[data-track-id="' + trackId + '"]');
            if (row) {
                var heart = row.querySelector('.lib-heart');
                if (heart) {
                    heart.innerHTML = data.favorited ? '&#10084;' : '&#9825;';
                    heart.classList.toggle('heart-active', !!data.favorited);
                }
            }
        }
    }).catch(function() {});
}

function _loadTrackFavorite(trackId) {
    // Loaded as part of track data, just check cache
    if (typeof _libTrackCache !== 'undefined' && _libTrackCache[trackId]) {
        _trackFavorites[trackId] = _libTrackCache[trackId].favorited || 0;
    }
}

// --- Stream heart (for non-library tracks that are being recorded) ---
// Heart/favorite state: supports both library tracks and stream favorites
var _streamHeartState = {trackId: null, fav: false, lastQuery: '', mode: null}; // mode: 'library' or 'stream'

var _castHeartCache = {}; // cacheKey -> {trackId, fav, mode, loading}

function _heartLookup(track, streamName, streamId, coverUrl, callback) {
    fetch('/api/library/track/find', {
        method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({track: track, stream_subdir: streamName})
    }).then(function(r) { return r.json(); })
    .then(function(data) {
        var mode = data.mode || 'stream';
        callback({trackId: data.id || null, fav: !!data.favorited, mode: mode});
    }).catch(function() { callback({trackId: null, fav: false, mode: 'stream'}); });
}

function _renderStreamHeartBtn() {
    var st = _lastStreamStatus[_playerStreamId] || {};
    var track = _browserIcyTrack || st.current_track || '';
    if (!track || track === 'recording' || track === '-' || track.replace(/[\s\-]/g, '') === '') return '';

    var streamName = st.stream_name || '';
    var queryKey = _playerStreamId + ':' + track;
    if (queryKey !== _streamHeartState.lastQuery) {
        _streamHeartState.lastQuery = queryKey;
        _streamHeartState.trackId = null;
        _streamHeartState.fav = false;
        _streamHeartState.mode = null;
        _heartLookup(track, streamName, _playerStreamId, st.cover_url || '', function(result) {
            _streamHeartState.trackId = result.trackId;
            _streamHeartState.fav = result.fav;
            _streamHeartState.mode = result.mode;
            _lastPlayerKey = '';
            _refreshPlayerBar();
        });
    }

    if (!_streamHeartState.mode) return '';
    var isFav = _streamHeartState.fav;
    return '<span class="player-heart' + (isFav ? ' heart-active' : '') + '" onclick="toggleStreamHeart()" title="Favorite" style="margin-left:12px;cursor:pointer;font-size:18px;user-select:none;">' + (isFav ? '&#10084;' : '&#9825;') + '</span>';
}

function _renderCastHeartBtn(streamId, track) {
    if (!track || track === '-' || track.replace(/[\s\-]/g, '') === '') return '';
    var st = _lastStreamStatus[streamId] || {};
    var streamName = st.stream_name || '';
    var cacheKey = streamId + ':' + track;
    var cached = _castHeartCache[cacheKey];

    if (!cached) {
        _castHeartCache[cacheKey] = {trackId: null, fav: false, mode: null, loading: true};
        _heartLookup(track, streamName, streamId, st.cover_url || '', function(result) {
            _castHeartCache[cacheKey] = {trackId: result.trackId, fav: result.fav, mode: result.mode};
            _lastPlayerKey = '';
            _refreshPlayerBar();
        });
        return '';
    }

    if (!cached.mode) return '';
    return '<span class="player-heart' + (cached.fav ? ' heart-active' : '') + '" onclick="toggleCastHeart(\'' + cacheKey.replace(/'/g, "\\'") + '\')" title="Favorite" style="margin-left:12px;cursor:pointer;font-size:18px;user-select:none;">' + (cached.fav ? '&#10084;' : '&#9825;') + '</span>';
}

function _toggleHeartCommon(mode, trackId, track, streamName, streamId, coverUrl, callback) {
    if (mode === 'library' && trackId) {
        fetch('/api/library/track/' + trackId + '/favorite', {
            method: 'POST', credentials: 'include'
        }).then(function(r) { return r.json(); })
        .then(function(data) { callback(!!data.favorited); })
        .catch(function() {});
    } else {
        fetch('/api/stream-favorites/toggle', {
            method: 'POST', credentials: 'include',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({track_name: track, stream_name: streamName, stream_id: streamId, cover_url: coverUrl})
        }).then(function(r) { return r.json(); })
        .then(function(data) { callback(data.favorited); })
        .catch(function() {});
    }
}

function toggleStreamHeart() {
    var st = _lastStreamStatus[_playerStreamId] || {};
    var track = _browserIcyTrack || st.current_track || '';
    var streamName = st.stream_name || '';
    var s = _streamHeartState;
    _toggleHeartCommon(s.mode, s.trackId, track, streamName, _playerStreamId, st.cover_url || '', function(fav) {
        _streamHeartState.fav = fav;
        _lastPlayerKey = '';
        _refreshPlayerBar();
    });
}

function toggleCastHeart(cacheKey) {
    var cached = _castHeartCache[cacheKey];
    if (!cached) return;
    var parts = cacheKey.split(':');
    var streamId = parts[0];
    var track = parts.slice(1).join(':');
    var st = _lastStreamStatus[streamId] || {};
    _toggleHeartCommon(cached.mode, cached.trackId, track, st.stream_name || '', streamId, st.cover_url || '', function(fav) {
        cached.fav = fav;
        _lastPlayerKey = '';
        _refreshPlayerBar();
    });
}

function _renderStarRating() {
    var trackId = (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : null;
    var rating = trackId ? (_trackRatings[trackId] || 0) : 0;
    var unusable = trackId ? (_trackUnusable[trackId] || 0) : 0;
    var html = '<span class="player-stars" style="margin-left:12px;" onmouseleave="_starHoverClear()">';
    html += '<span class="unusable-btn' + (unusable ? ' unusable-active' : '') + '" onclick="toggleUnusable()" title="Unusable for mixing"><svg width="14" height="14" viewBox="0 0 24 24" style="vertical-align:middle;"><path d="M22 4h-2c-.55 0-1 .45-1 1v9c0 .55.45 1 1 1h2V4zM2.17 11.12c-.11.25-.17.52-.17.8V13c0 1.1.9 2 2 2h5.5l-.92 4.65c-.05.22-.02.46.08.66.23.45.52.86.88 1.22L10 22l6.41-6.41c.38-.38.59-.89.59-1.42V6.34C17 5.05 15.95 4 14.66 4H6.82c-.77 0-1.45.47-1.73 1.18L2.17 11.12z" fill="currentColor"/></svg></span>';
    for (var i = 1; i <= 5; i++) {
        var filled = i <= rating;
        html += '<span class="star-btn' + (filled ? ' star-filled' : '') + '" data-star="' + i + '" onclick="setTrackRating(' + i + ')" onmouseenter="_starHover(' + i + ')" title="' + i + '/5">&#9733;</span>';
    }
    html += '</span>';
    return html;
}

function _starHover(n) {
    var stars = document.querySelectorAll('.player-stars .star-btn');
    for (var i = 0; i < stars.length; i++) {
        if (i < n) stars[i].classList.add('star-hover');
        else stars[i].classList.remove('star-hover');
    }
}

function _starHoverClear() {
    var stars = document.querySelectorAll('.player-stars .star-btn');
    for (var i = 0; i < stars.length; i++) {
        stars[i].classList.remove('star-hover');
    }
}

// --- Player playlist button + track playlist info ---
var _trackPlaylists = {}; // {trackId: [{id, name}, ...]}

function _renderPlayerPlaylistBtn() {
    var trackId = (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : null;
    if (!trackId) return '';
    var html = '<button class="player-btn player-pl-btn" onclick="_showPlayerPlaylistMenu(this)" title="' + t('library.add_to_playlist') + '" style="margin-left:12px;">+ PLAYLIST</button>';
    // Show playlists this track is in
    var pls = _trackPlaylists[trackId] || [];
    if (pls.length) {
        html += '<span class="player-pl-tags">';
        for (var i = 0; i < pls.length; i++) {
            var plColor = pls[i].color || '';
            var tagStyle = plColor ? 'background:' + plColor + ';color:#fff;border-color:' + plColor + ';' : '';
            html += '<span class="player-pl-tag" style="' + tagStyle + '" oncontextmenu="_removeFromPlaylistTag(' + pls[i].id + ',event);return false;" title="Right-click to remove">' + _escHtmlPlayer(pls[i].name) + '</span>';
        }
        html += '</span>';
    }
    return html;
}

// --- Player cast button ---
var _playerCastMenu = null;

function _renderPlayerCastBtn() {
    return '<button class="player-btn player-cast-btn" onclick="_showPlayerCastMenu(this)" title="Cast" style="margin-left:12px;">'
        + '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        + '<path d="M2 16.1A5 5 0 0 1 5.9 20M2 12.05A9 9 0 0 1 9.95 20M2 8V6a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-6"/>'
        + '<line x1="2" y1="20" x2="2.01" y2="20"/></svg></button>';
}

function _showPlayerCastMenu(btn) {
    if (_playerCastMenu) { _playerCastMenu.remove(); _playerCastMenu = null; return; }

    var trackId = (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : null;
    if (!trackId) return;

    var menu = document.createElement('div');
    menu.className = 'cast-menu';
    menu.style.position = 'fixed';
    menu.style.zIndex = '3000';

    var rect = btn.getBoundingClientRect();
    menu.style.left = rect.left + 'px';
    menu.style.bottom = (window.innerHeight - rect.top + 4) + 'px';

    var html = '';
    var isBrowser = !_libCastDeviceIds || _libCastDeviceIds.length === 0;
    // Browser option
    html += '<button class="cast-menu-item' + (isBrowser ? ' cast-menu-active' : '') + '" onclick="castLibraryTrack(\'browser\')">'
        + '&#9654; Browser <span class="cast-device-badge" style="background:#666;">Local</span></button>';
    if (_castDevicesCache && _castDevicesCache.length > 0) {
        _castDevicesCache.forEach(function(d) {
            if (d.enabled === false) return;
            var isActive = _libCastDeviceIds && _libCastDeviceIds.indexOf(d.id) !== -1;
            var badge = d.type === 'lms'
                ? '<span class="cast-device-badge badge-lms">LMS</span>'
                : '<span class="cast-device-badge badge-sonos">Sonos</span>';
            // Click on active device = stop that device, click on inactive = add it
            var onclick = isActive ? 'castLibraryTrackStop(\'' + d.id + '\')' : 'castLibraryTrack(\'' + d.id + '\')';
            html += '<button class="cast-menu-item' + (isActive ? ' cast-menu-active' : '') + '" onclick="' + onclick + '">'
                + (isActive ? '&#9632; ' : '&#9654; ') + d.name + badge + '</button>';
        });
    }

    menu.innerHTML = html;
    document.body.appendChild(menu);
    _playerCastMenu = menu;
}

function castLibraryTrack(deviceId) {
    if (_playerCastMenu) { _playerCastMenu.remove(); _playerCastMenu = null; }
    var trackId = (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : null;
    if (!trackId) return;

    // If selecting "browser", stop all casts and resume browser playback
    if (deviceId === 'browser') {
        // Calculate current cast position before clearing state
        var castPosition = 0;
        if (_castPlayStart && _castTrackDuration > 0) {
            castPosition = (Date.now() / 1000) - _castPlayStart;
            if (castPosition > _castTrackDuration) castPosition = _castTrackDuration;
            if (castPosition < 0) castPosition = 0;
        }
        _libCastDeviceIds.forEach(function(did) {
            fetch('/api/cast/stop', {
                method: 'POST', credentials: 'include',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({device_id: did})
            }).catch(function() {});
        });
        _libCastDeviceId = null;
        _libCastDeviceName = '';
        _libCastDeviceIds = [];
        _stopCastPoll();
        _castPlayStart = 0;
        _castTrackDuration = 0;
        sessionStorage.removeItem('_libCastDeviceIds');
        sessionStorage.removeItem('_castPlayStart');
        // Resume browser audio at the cast position
        if (_playerAudio && _libPlayingTrackId) {
            var trackUrl = '/api/library/track/' + _libPlayingTrackId + '/play';
            if (!_playerAudio.src || !_playerAudio.src.includes('/play')) {
                _initBrowserAudio();
                _playerAudio.src = trackUrl;
            }
            _playerAudio.currentTime = castPosition;
            _playerAudio.play().catch(function() {});
            _browserPaused = false;
        }
        _refreshPlayerBar();
        return;
    }

    // Add cast device (don't stop previous — allow multi-cast)
    fetch('/api/cast/play-library', {
        method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({track_id: trackId, device_id: deviceId})
    }).then(function(r) { return r.json(); })
    .then(function(data) {
        if (!data.success) { alert(data.message || data.error); return; }
        // Pause browser audio — track plays on cast device(s)
        if (_playerAudio && !_playerAudio.paused) {
            _playerAudio.pause();
            _browserPaused = true;
        }
        // Add to active cast devices list
        if (!_libCastDeviceIds) _libCastDeviceIds = [];
        if (_libCastDeviceIds.indexOf(deviceId) === -1) _libCastDeviceIds.push(deviceId);
        // Primary device for volume control = most recently added
        _libCastDeviceId = deviceId;
        _libCastDeviceName = deviceId;
        if (_castDevicesCache) {
            for (var i = 0; i < _castDevicesCache.length; i++) {
                if (_castDevicesCache[i].id === deviceId) {
                    _libCastDeviceName = _castDevicesCache[i].name;
                    break;
                }
            }
        }
        // _startCastPoll calls _stopCastPoll which resets timing — so set values AFTER
        _startCastPoll();
        var _tid = (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : null;
        var _tr = (_tid && typeof _libTrackCache !== 'undefined') ? _libTrackCache[_tid] : null;
        _castPlayStart = Date.now() / 1000;
        _castTrackDuration = (_tr && _tr.duration_sec) ? _tr.duration_sec : 0;
        // Persist cast state for page reload
        sessionStorage.setItem('_libCastDeviceIds', JSON.stringify(_libCastDeviceIds));
        sessionStorage.setItem('_castPlayStart', String(_castPlayStart));
        // Fetch device volume and update player
        fetch('/api/cast/volume/' + encodeURIComponent(deviceId), {credentials: 'include'})
            .then(function(r) { return r.json(); })
            .then(function(vdata) {
                if (vdata.volume != null) {
                    _volState[deviceId] = {value: vdata.volume, dragging: false};
                }
                _refreshPlayerBar();
            })
            .catch(function() { _refreshPlayerBar(); });
    }).catch(function() {});
}

function _getLibCastNames() {
    if (!_libCastDeviceIds || !_castDevicesCache) return '';
    var names = _libCastDeviceIds.map(function(did) {
        for (var i = 0; i < _castDevicesCache.length; i++) {
            if (_castDevicesCache[i].id === did) return _escHtmlPlayer(_castDevicesCache[i].name);
        }
        return _escHtmlPlayer(did);
    });
    return names.join(', ');
}

function stopAllLibCasts() {
    if (_libCastDeviceIds && _libCastDeviceIds.length > 0) {
        _libCastDeviceIds.forEach(function(did) {
            fetch('/api/cast/stop', {
                method: 'POST', credentials: 'include',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({device_id: did})
            }).catch(function() {});
        });
    }
    _libCastDeviceId = null;
    _libCastDeviceName = '';
    _libCastDeviceIds = [];
    _stopCastPoll();
    _libPlayingTrackId = null;
    sessionStorage.removeItem('_libPlayingTrackId');
    sessionStorage.removeItem('_browserLibStream');
    _refreshPlayerBar();
    if (typeof updatePlayButtons === 'function') updatePlayButtons();
}

function castLibraryTrackStop(deviceId) {
    if (_playerCastMenu) { _playerCastMenu.remove(); _playerCastMenu = null; }
    // Stop this specific device
    fetch('/api/cast/stop', {
        method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({device_id: deviceId})
    }).catch(function() {});
    // Remove from active list
    _libCastDeviceIds = _libCastDeviceIds.filter(function(id) { return id !== deviceId; });
    // If no more cast devices, switch to browser playback at current position
    if (_libCastDeviceIds.length === 0) {
        // Calculate current cast position before clearing state
        var castPosition = 0;
        if (_castPlayStart && _castTrackDuration > 0) {
            castPosition = (Date.now() / 1000) - _castPlayStart;
            if (castPosition > _castTrackDuration) castPosition = _castTrackDuration;
            if (castPosition < 0) castPosition = 0;
        }
        _libCastDeviceId = null;
        _libCastDeviceName = '';
        _stopCastPoll();
        _castPlayStart = 0;
        _castTrackDuration = 0;
        sessionStorage.removeItem('_libCastDeviceIds');
        sessionStorage.removeItem('_castPlayStart');
        // Resume in browser at the cast position
        if (_playerAudio && _libPlayingTrackId) {
            var trackUrl = '/api/library/track/' + _libPlayingTrackId + '/play';
            if (!_playerAudio.src || !_playerAudio.src.includes('/play')) {
                _initBrowserAudio();
                _playerAudio.src = trackUrl;
            }
            _playerAudio.currentTime = castPosition;
            _playerAudio.play().catch(function() {});
            _browserPaused = false;
        }
    } else {
        // Switch primary to remaining device
        _libCastDeviceId = _libCastDeviceIds[_libCastDeviceIds.length - 1];
        _libCastDeviceName = _libCastDeviceId;
        if (_castDevicesCache) {
            for (var i = 0; i < _castDevicesCache.length; i++) {
                if (_castDevicesCache[i].id === _libCastDeviceId) {
                    _libCastDeviceName = _castDevicesCache[i].name;
                    break;
                }
            }
        }
    }
    _refreshPlayerBar();
}

// Close cast menu on outside click
document.addEventListener('click', function(e) {
    if (_playerCastMenu && !_playerCastMenu.contains(e.target) && !e.target.closest('.player-cast-btn')) {
        _playerCastMenu.remove();
        _playerCastMenu = null;
    }
});

function _renderPlayerTrashBtn() {
    var trackId = (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : null;
    if (!trackId) return '';
    return '<button class="player-btn player-trash-btn" onclick="_showPlayerTrashMenu(this)" title="Delete" style="margin-left:12px;">&#128465;</button>';
}

var _playerTrashMenu = null;

function _showPlayerTrashMenu(btn) {
    _closePlayerTrashMenu();
    var trackId = _libPlayingTrackId;
    if (!trackId) return;
    var menu = document.createElement('div');
    menu.className = 'lib-pl-menu';
    menu.style.zIndex = '10001';
    menu.innerHTML =
        '<button class="lib-pl-menu-item" onclick="_playerTrashAction(' + trackId + ', false)">' +
        '&#128465; ' + t('library.trash_keep') + '</button>' +
        '<button class="lib-pl-menu-item" style="color:#f44;" onclick="_playerTrashAction(' + trackId + ', true)">' +
        '&#128465; ' + t('library.trash_full') + '</button>';
    var rect = btn.getBoundingClientRect();
    menu.style.position = 'fixed';
    menu.style.left = (rect.left - 200) + 'px';
    menu.style.top = (rect.top - 80) + 'px';
    document.body.appendChild(menu);
    _playerTrashMenu = menu;
}

function _closePlayerTrashMenu() {
    if (_playerTrashMenu) { _playerTrashMenu.remove(); _playerTrashMenu = null; }
}

function _playerTrashAction(trackId, fullDelete) {
    _closePlayerTrashMenu();
    var endpoint = fullDelete ? '/api/library/track/' + trackId + '/delete' : '/api/library/track/' + trackId + '/trash';
    fetch(endpoint, { method: 'POST', credentials: 'include' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                // Stop current playback
                if (_playerAudio) { _playerAudio.pause(); _playerAudio.src = ''; }
                // Remove from list if visible
                var row = document.querySelector('tr[data-track-id="' + trackId + '"]');
                if (row) row.remove();
                // Play next track based on shuffle/repeat mode
                if (_libShuffleMode) {
                    _playRandomTrack();
                } else if (_libRepeatMode === 'all') {
                    _libPlayNextTrack(true);
                } else {
                    _libPlayNextTrack(false);
                }
            }
        });
}

document.addEventListener('click', function(e) {
    if (_playerTrashMenu && !_playerTrashMenu.contains(e.target) && !e.target.classList.contains('player-trash-btn')) {
        _closePlayerTrashMenu();
    }
});

function _loadTrackPlaylists(trackId) {
    fetch('/api/library/track/' + trackId + '/playlists', {credentials: 'include'})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.playlists) {
                _trackPlaylists[trackId] = data.playlists;
                _refreshPlayerBar();
            }
        })
        .catch(function() {});
}

function _showPlayerPlaylistMenu(btn) {
    var trackId = (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : null;
    if (!trackId) return;
    // Remove existing menu
    var old = document.querySelector('.player-pl-menu');
    if (old) { old.remove(); return; }

    fetch('/api/library/playlists', {credentials: 'include'})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var pls = data.playlists || [];
            if (!pls.length) return;
            var current = (_trackPlaylists && _trackPlaylists[trackId]) || [];
            function _isIn(plId) {
                for (var k = 0; k < current.length; k++) if (current[k].id === plId) return true;
                return false;
            }
            var menu = document.createElement('div');
            menu.className = 'player-pl-menu';
            for (var i = 0; i < pls.length; i++) {
                var item = document.createElement('button');
                item.className = 'player-pl-menu-item';
                var active = _isIn(pls[i].id);
                if (active) {
                    item.classList.add('active');
                    item.innerHTML = '<span style="color:#4caf50;margin-right:0.4rem;">&#10003;</span>' + pls[i].name.replace(/[&<>"']/g, function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});
                } else {
                    item.textContent = pls[i].name;
                }
                item.setAttribute('data-pl-id', pls[i].id);
                item.onclick = (function(plId, isActive) {
                    return function() {
                        if (isActive) {
                            fetch('/api/library/playlists/' + plId + '/tracks/' + trackId, {method: 'DELETE', credentials: 'include'})
                                .then(function() {
                                    _loadTrackPlaylists(trackId);
                                    if (typeof _updateListPlaylistTags === 'function') _updateListPlaylistTags([trackId]);
                                    if (typeof loadPlaylists === 'function') loadPlaylists();
                                });
                        } else {
                            _addTrackToPlaylistFromPlayer(trackId, plId);
                        }
                        menu.remove();
                    };
                })(pls[i].id, active);
                menu.appendChild(item);
            }
            // Position above the button
            var rect = btn.getBoundingClientRect();
            menu.style.position = 'fixed';
            menu.style.left = rect.left + 'px';
            menu.style.bottom = (window.innerHeight - rect.top + 4) + 'px';
            document.body.appendChild(menu);
            // Close on outside click
            setTimeout(function() {
                document.addEventListener('click', function _closeMenu(e) {
                    if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener('click', _closeMenu); }
                });
            }, 0);
        });
}

function _removeFromPlaylistTag(playlistId, e) {
    e.preventDefault();
    var trackId = (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : null;
    if (!trackId) return;
    fetch('/api/library/playlists/' + playlistId + '/tracks/' + trackId, {
        method: 'DELETE', credentials: 'include',
    })
    .then(function() {
        _loadTrackPlaylists(trackId);
        if (typeof loadPlaylists === 'function') loadPlaylists();
        if (typeof _updateListPlaylistTags === 'function') _updateListPlaylistTags([trackId]);
    })
    .catch(function() {});
}

function _addTrackToPlaylistFromPlayer(trackId, playlistId) {
    fetch('/api/library/playlists/' + playlistId + '/tracks', {
        method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({track_ids: [trackId]}),
    })
    .then(function() {
        _loadTrackPlaylists(trackId);
        if (typeof loadPlaylists === 'function') loadPlaylists();
        if (typeof _updateListPlaylistTags === 'function') _updateListPlaylistTags([trackId]);
    })
    .catch(function() {});
}

function setTrackRating(rating) {
    var trackId = (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : null;
    if (!trackId) return;
    // Cannot rate if marked unusable
    if (_trackUnusable[trackId]) return;
    // Toggle off if clicking same rating
    if (_trackRatings[trackId] === rating) rating = 0;
    _trackRatings[trackId] = rating;
    // Update cache
    if (typeof _libTrackCache !== 'undefined' && _libTrackCache[trackId]) {
        _libTrackCache[trackId].rating = rating;
    }
    fetch('/api/library/track/' + trackId + '/rating', {
        method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({rating: rating}),
    }).catch(function() {});
    _refreshPlayerBar();
    // Update list stars if visible
    var row = document.querySelector('#lib-table tr[data-track-id="' + trackId + '"]');
    if (row) {
        var cell = row.querySelector('.col-rating');
        if (cell && typeof _renderListStars === 'function') cell.innerHTML = _renderListStars(trackId, rating);
    }
}

function toggleUnusable() {
    var trackId = (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : null;
    if (!trackId) return;
    // Cannot activate if rating > 0
    if (!_trackUnusable[trackId] && (_trackRatings[trackId] || 0) > 0) return;
    var newVal = _trackUnusable[trackId] ? 0 : 1;
    _trackUnusable[trackId] = newVal;
    if (typeof _libTrackCache !== 'undefined' && _libTrackCache[trackId]) {
        _libTrackCache[trackId].unusable = newVal;
    }
    fetch('/api/library/track/' + trackId + '/unusable', {
        method: 'POST', credentials: 'include',
    }).catch(function() {});
    _refreshPlayerBar();
    if (typeof _vsRenderedStart !== 'undefined') { _vsRenderedStart = -1; _vsRenderVisible(); }
}

function toggleListUnusable(trackId, e) {
    e.stopPropagation();
    var tr = _libTrackCache[trackId];
    var newVal = (tr && tr.unusable) ? 0 : 1;
    if (tr) tr.unusable = newVal;
    _trackUnusable[trackId] = newVal;
    // Unusable overrides rating: clear stars when marking unusable
    if (newVal) {
        if (tr) tr.rating = 0;
        _trackRatings[trackId] = 0;
        fetch('/api/library/track/' + trackId + '/rate', {
            method: 'POST', credentials: 'include',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({rating: 0})
        }).catch(function() {});
    }
    fetch('/api/library/track/' + trackId + '/unusable', {
        method: 'POST', credentials: 'include',
    }).catch(function() {});
    if (_libPlayingTrackId === trackId) _refreshPlayerBar();
    if (typeof _vsRenderedStart !== 'undefined') { _vsRenderedStart = -1; _vsRenderVisible(); }
}

function _loadTrackRating(trackId) {
    fetch('/api/library/track/' + trackId, {credentials: 'include'})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data) {
                if (data.rating != null) _trackRatings[trackId] = data.rating;
                if (data.unusable != null) _trackUnusable[trackId] = data.unusable;
                if (data.favorited != null) _trackFavorites[trackId] = data.favorited;
                _refreshPlayerBar();
            }
        })
        .catch(function() {});
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

// --- Keyboard controls ---
document.addEventListener('keydown', function(e) {
    // Ignore if typing in an input/textarea/select
    var tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return;

    if (e.code === 'Space') {
        e.preventDefault();
        // Play/pause works normally (also inside loop)
        if (_isLibraryTrack || _playerStreamId) {
            toggleBrowserPause();
        }
    }

    if (e.code === 'Escape' && _loopMode) {
        e.preventDefault();
        _loopMode = false;
        _stopLoopEnforce();
        if (_playerAudio && _isLibraryTrack) {
            _playerAudio.play().catch(function() {});
            _browserPaused = false;
            _refreshPlayerBar();
        }
    }

    // Left/Right: jump _seekStep seconds in track
    if ((e.code === 'ArrowLeft' || e.code === 'ArrowRight') && _isLibraryTrack && _playerAudio) {
        e.preventDefault();
        var dur = (_playerAudio.duration && isFinite(_playerAudio.duration)) ? _playerAudio.duration : _libTrackDuration;
        if (e.code === 'ArrowLeft') {
            _playerAudio.currentTime = Math.max(0, _playerAudio.currentTime - _seekStep);
        } else {
            _playerAudio.currentTime = Math.min(dur, _playerAudio.currentTime + _seekStep);
        }
    }

    // Up/Down: adjust seek step (2-10 in 1s steps, 10-30 in 5s steps)
    if ((e.code === 'ArrowUp' || e.code === 'ArrowDown') && _isLibraryTrack) {
        e.preventDefault();
        if (e.code === 'ArrowUp') {
            if (_seekStep < 10) _seekStep = Math.min(10, _seekStep + 1);
            else _seekStep = Math.min(30, _seekStep + 5);
        } else {
            if (_seekStep <= 10) _seekStep = Math.max(2, _seekStep - 1);
            else _seekStep = Math.max(10, _seekStep - 5);
        }
        var valEl = document.querySelector('.player-volume-value');
        if (valEl) valEl.textContent = _seekStep + 's';
    }

    // 0: insert cue at current position (auto-numbered chronologically)
    if (_isLibraryTrack && _playerAudio && !e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey && e.key === '0') {
        e.preventDefault();
        cueInsert();
    }

    // Number keys 1-8: jump to existing cue; Shift+1-8: delete cue
    // Use e.code (KeyDigit1-8) to work regardless of keyboard layout (Shift+1 = '!' on DE keyboards)
    if (_isLibraryTrack && _playerAudio && !e.ctrlKey && !e.altKey && !e.metaKey) {
        var codeMatch = e.code && e.code.match(/^Digit([1-8])$/);
        if (codeMatch) {
            e.preventDefault();
            var cueNum = parseInt(codeMatch[1]);
            if (e.shiftKey) {
                cueClear(cueNum, e);
            } else {
                cueAction(cueNum);
            }
        }
    }

    // +/- keys: adjust loop length
    if ((e.key === '+' || e.key === '=') && _loopMode && _isLibraryTrack) {
        e.preventDefault();
        _loopLength = Math.min(10, _loopLength * 2);
    }
    if ((e.key === '-' || e.key === '_') && _loopMode && _isLibraryTrack) {
        e.preventDefault();
        _loopLength = Math.max(0.05, _loopLength / 2);
    }
});

var _loopTimer = null;

function _loopEnforce() {
    if (!_loopMode || !_playerAudio) {
        _loopTimer = null;
        return;
    }
    if (_playerAudio.currentTime >= _loopStart + _loopLength) {
        _playerAudio.currentTime = _loopStart;
    }
}

function _startLoopEnforce() {
    if (_loopTimer) clearInterval(_loopTimer);
    _loopTimer = setInterval(_loopEnforce, 50);
}

function _stopLoopEnforce() {
    if (_loopTimer) { clearInterval(_loopTimer); _loopTimer = null; }
}

// --- Seek slider for library tracks + browse streams ---
function _onSeekUpdate() {
    var _isBrowseSeekable = !_isLibraryTrack && _playerStreamId === 'browse' && _playerAudio && _playerAudio.duration && isFinite(_playerAudio.duration) && _playerAudio.duration > 0;
    if (_seekDragging || (!_isLibraryTrack && !_isBrowseSeekable) || !_playerAudio) return;
    var dur = (_playerAudio.duration && isFinite(_playerAudio.duration)) ? _playerAudio.duration : _libTrackDuration;
    var cur = _playerAudio.currentTime || 0;
    if (!dur || !isFinite(dur)) return;
    var pct = (cur / dur) * 100;
    var fill = document.getElementById('seek-fill');
    var handle = document.getElementById('seek-handle');
    var timeEl = document.getElementById('seek-time');
    if (fill) fill.style.width = pct + '%';
    if (handle) handle.style.left = pct + '%';
    if (timeEl) timeEl.textContent = _fmtTime(cur);
    var durEl = document.getElementById('seek-dur');
    if (durEl) durEl.textContent = _fmtTime(dur);
    var remEl = document.getElementById('seek-remaining');
    if (remEl) remEl.textContent = '-' + _fmtTime(dur - cur);
    _updateCueMarkers(dur);
    _scheduleWaveformRedraw();
    if (typeof AutoDJ !== 'undefined' && AutoDJ.enabled) AutoDJ._checkFade();
}

function _updateCueMarkers(dur) {
    var bar = document.getElementById('seek-bar');
    if (!bar || !dur || !isFinite(dur)) return;
    var tid = (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : null;
    var trackCues = tid ? (_cuePoints[tid] || {}) : {};
    // Check if markers need updating
    var existing = bar.querySelectorAll('.cue-marker');
    var needsUpdate = existing.length === 0 && Object.keys(trackCues).length > 0;
    if (!needsUpdate) {
        // Check if cue count changed
        var cueCount = 0;
        for (var k in trackCues) { if (trackCues[k] != null) cueCount++; }
        if (existing.length !== cueCount) needsUpdate = true;
    }
    if (!needsUpdate) return;
    // Remove old markers
    existing.forEach(function(m) { m.remove(); });
    // Add new markers
    for (var cn = 1; cn <= 8; cn++) {
        if (trackCues[cn] != null) {
            var pct = (trackCues[cn] / dur * 100);
            var marker = document.createElement('div');
            marker.className = 'cue-marker';
            marker.setAttribute('data-cue', cn);
            marker.style.left = pct + '%';
            marker.style.background = _CUE_COLORS[cn - 1];
            bar.appendChild(marker);
        }
    }
}

function _fmtTime(s) {
    var m = Math.floor(s / 60);
    var sec = Math.floor(s % 60);
    return m + ':' + (sec < 10 ? '0' : '') + sec;
}

function _seekFromEvent(e) {
    var bar = document.getElementById('seek-bar');
    if (!bar) return;
    var rect = bar.getBoundingClientRect();
    var x = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
    var pct = Math.max(0, Math.min(1, x / rect.width));
    var _isCastSeek = _libCastDeviceIds && _libCastDeviceIds.length > 0;
    if (_isCastSeek && _castTrackDuration > 0) {
        _lastSeekTime = Date.now();
        var pos = pct * _castTrackDuration;
        // Seek on cast device
        fetch('/api/cast/seek', {
            method: 'POST', credentials: 'include',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({device_id: _libCastDeviceIds[0], position: pos})
        }).catch(function() {});
        // Update local cast timing to reflect new position
        _castPlayStart = (Date.now() / 1000) - pos;
        sessionStorage.setItem('_castPlayStart', String(_castPlayStart));
    } else if (_playerAudio) {
        var dur = (_playerAudio.duration && isFinite(_playerAudio.duration)) ? _playerAudio.duration : _libTrackDuration;
        if (dur > 0) {
            _lastSeekTime = Date.now();
            _playerAudio.currentTime = pct * dur;
        }
    }
    var fill = document.getElementById('seek-fill');
    var handle = document.getElementById('seek-handle');
    if (fill) fill.style.width = (pct * 100) + '%';
    if (handle) handle.style.left = (pct * 100) + '%';
    if (typeof _drawWaveform === 'function') _drawWaveform();
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
function _loadWaveformById(trackId) {
    var key = 'track:' + trackId;
    if (_waveformTrackUrl === key && _waveformData) {
        _drawWaveform();
        return;
    }
    _waveformData = null;
    _waveformTrackUrl = key;
    fetch('/api/library/track/' + trackId + '/waveform', {credentials: 'include'})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.peaks && data.peaks.length) {
                _waveformData = new Float32Array(data.peaks);
                _drawWaveform();
            }
        })
        .catch(function() { _waveformData = null; });
}

function _loadWaveform(url) {
    if (_waveformTrackUrl === url && _waveformData) {
        _drawWaveform();
        return;
    }
    _waveformData = null;
    _waveformTrackUrl = url;
    // Extract track ID from URL: /api/library/track/{id}/play
    var match = url.match(/\/api\/library\/track\/(\d+)\/play/);
    if (!match) return;
    var trackId = match[1];
    // Fetch pre-computed waveform from server (lightweight JSON)
    fetch('/api/library/track/' + trackId + '/waveform', {credentials: 'include'})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.peaks && data.peaks.length) {
                _waveformData = new Float32Array(data.peaks);
                _drawWaveform();
            }
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
    var _isCastWf = _libCastDeviceIds && _libCastDeviceIds.length > 0;
    if (_isCastWf && _castPlayStart && _castTrackDuration > 0) {
        var elapsed = (Date.now() / 1000) - _castPlayStart;
        if (elapsed > _castTrackDuration) elapsed = _castTrackDuration;
        progress = elapsed / _castTrackDuration;
    } else {
        var _wfDur = (_playerAudio && _playerAudio.duration && isFinite(_playerAudio.duration)) ? _playerAudio.duration : _libTrackDuration;
        if (_playerAudio && _wfDur > 0) {
            progress = _playerAudio.currentTime / _wfDur;
        }
    }

    for (var i = 0; i < bars; i++) {
        var val = data[i];
        var barH = Math.max(1, val * (h - 2));
        var x = i * barW;
        var pct = i / bars;
        if (pct < progress) {
            ctx.fillStyle = '#42a5f5';
        } else {
            ctx.fillStyle = 'rgba(66,165,245,0.3)';
        }
        ctx.fillRect(x, (h - barH) / 2, Math.max(1, barW - 1), barH);
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

var _lastPlayerKey = '';

var _refreshPlayerBarTimer = null;
function _refreshPlayerBar() {
    if (_refreshPlayerBarTimer) return;
    _refreshPlayerBarTimer = setTimeout(function() {
        _refreshPlayerBarTimer = null;
        _refreshPlayerBarNow();
    }, 50);
}
function _refreshPlayerBarNow() {
    var container = document.getElementById('player-container');
    if (!container) return;

    var hasCast = _playerData && _playerData.active && _playerData.players && _playerData.players.length > 0;
    var hasBrowser = !!_playerStreamId || _isLibraryTrack;
    var totalPlayers = 0;

    // Build a key to detect if the player state actually changed
    var playerKey = [
        hasCast ? 'c' : '', hasBrowser ? 'b' : '',
        _playerStreamId, _isLibraryTrack, _browseStreamName,
        (_playerAudio && _playerAudio.duration && isFinite(_playerAudio.duration)) ? 'dur' : '',
        (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : '',
        _browserPaused, _browserIcyTrack, _browserIcyCover, _browserLibStream,
        _libRepeatMode, Math.round(_browserVolume * 100), _seekStep, (typeof AutoDJ !== 'undefined' ? AutoDJ.enabled : ''),
        JSON.stringify(_trackRatings[(typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : 0] || 0),
        (_trackUnusable[(typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : 0] || 0),
        JSON.stringify(_trackPlaylists[(typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : 0] || []),
        JSON.stringify(_cuePoints[(typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : 0] || {}),
        _loopMode, _libCastDeviceId || '',
        hasCast ? JSON.stringify(_playerData.players.map(function(p){return p.device_id+':'+p.stream_id})) : '',
    ].join('|');

    if (playerKey === _lastPlayerKey && container.innerHTML !== '') {
        return; // Nothing changed, skip re-render
    }
    _lastPlayerKey = playerKey;

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

        var streamPlayers = _playerData.players.filter(function(p) { return !p.is_library; }).slice(0, MAX_CASTS);
        streamPlayers.forEach(function(p, idx) {
            html += _renderPlayerHTML(p, idx);
        });
        // Library cast players are controlled via the browser player bar — no separate bar
        totalPlayers += streamPlayers.length;

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

    // Init seek drag + visualizer for library tracks and seekable browse streams
    var _browseSeekable = !_isLibraryTrack && _playerStreamId === 'browse' && _playerAudio && _playerAudio.duration && isFinite(_playerAudio.duration) && _playerAudio.duration > 0;
    if (hasBrowser && (_isLibraryTrack || _browseSeekable)) {
        _initSeekDrag();
        var _isCastingNow = _libCastDeviceIds && _libCastDeviceIds.length > 0;
        if (_isCastingNow && _libPlayingTrackId) {
            // Cast mode: load waveform by track ID, redraw existing data
            if (_waveformData) {
                _drawWaveform();
            } else {
                _loadWaveformById(_libPlayingTrackId);
            }
        } else if (_playerAudio && _playerAudio.src) {
            _loadWaveform(_playerAudio.src);
        }
    } else {
        _waveformData = null;
    }

    if (_multiroomOpen && hasCast) _renderMultiroomPanel(_playerData);
}

function _updateVersionPosition(playerCount) {
    // No-op: flex layout handles spacing automatically
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
    if (!_playerStreamId || _isLibraryTrack || _playerStreamId === 'browse') { if (!_isLibraryTrack && _playerStreamId !== 'browse') { _browserIcyTrack = ''; _browserIcyCover = null; } return; }
    // Bookmark streams use generic ICY endpoint with URL
    var icyUrl;
    if (String(_playerStreamId).indexOf('bm-') === 0 && _playerStreamUrl) {
        // Extract original URL from proxy URL
        var origUrl = _playerStreamUrl;
        if (origUrl.indexOf('/api/listen?url=') === 0) {
            origUrl = decodeURIComponent(origUrl.replace('/api/listen?url=', ''));
        }
        icyUrl = '/api/icy?url=' + encodeURIComponent(origUrl);
    } else {
        icyUrl = '/api/stream/' + _playerStreamId + '/icy';
    }
    fetch(icyUrl, {credentials: 'include'})
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

// Start polling (10s interval to reduce load)
setInterval(updateStatus, 10000);
updateStatus();
