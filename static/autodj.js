/**
 * Auto-DJ Module for Streampeg
 * Smart track selection based on BPM compatibility and Key harmony (Camelot wheel).
 * Stufe 1: Smart queue + simple volume crossfade. No beatmatching.
 */

var AutoDJ = {
    enabled: localStorage.getItem('_autoDJEnabled') === '1',
    _history: [],
    _fadeDuration: parseInt(localStorage.getItem('_autoDJFade') || '10') * 1000,
    _fading: false,
    _originalVolume: null,
    _fadeInterval: null,
    _fadeAudio: null,
    _nextTrackId: null,
    _crossfadeComplete: false,
    _debug: true,
    _debugLog: [],

    _log: function(msg) {
        if (!AutoDJ._debug) return;
        var ts = new Date().toLocaleTimeString();
        var line = ts + ' ' + msg;
        AutoDJ._debugLog.push(line);
        if (AutoDJ._debugLog.length > 200) AutoDJ._debugLog.shift();
        // Send to server log
        fetch('/api/autodj/log', {
            method: 'POST', credentials: 'include',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({msg: line})
        }).catch(function() {});
    },

    // --- Public API ---

    toggle: function() {
        AutoDJ.enabled = !AutoDJ.enabled;
        localStorage.setItem('_autoDJEnabled', AutoDJ.enabled ? '1' : '0');
        // Auto-DJ and shuffle are mutually exclusive
        if (AutoDJ.enabled && typeof _libShuffleMode !== 'undefined' && _libShuffleMode) {
            _libShuffleMode = false;
            localStorage.setItem('_libShuffleMode', '0');
        }
        if (!AutoDJ.enabled) {
            AutoDJ._resetFade();
            AutoDJ._history = [];
        }
        AutoDJ._updateButton();
        if (typeof _refreshPlayerBar === 'function') _refreshPlayerBar();
    },

    _updateButton: function() {
        var btn = document.getElementById('lib-autodj-btn');
        if (!btn) return;
        if (AutoDJ.enabled) {
            btn.style.color = '#42a5f5';
            btn.style.borderColor = '#42a5f5';
        } else {
            btn.style.color = '#666';
            btn.style.borderColor = '#666';
        }
    },

    getNextTrack: function(currentTrackId, availableTracks) {
        if (!availableTracks || !availableTracks.length) return null;

        var current = null;
        if (typeof _libTrackCache !== 'undefined' && _libTrackCache[currentTrackId]) {
            current = _libTrackCache[currentTrackId];
        } else {
            for (var i = 0; i < availableTracks.length; i++) {
                if (availableTracks[i].id === currentTrackId) {
                    current = availableTracks[i];
                    break;
                }
            }
        }

        // Build set of played titles (normalized) to avoid duplicates with different IDs
        var playedTitles = {};
        if (current) {
            var curKey = ((current.artist || '') + '|' + (current.title || '')).toLowerCase().replace(/[^a-z0-9]/g, '');
            playedTitles[curKey] = true;
        }
        for (var h = 0; h < AutoDJ._history.length; h++) {
            var ht = (typeof _libTrackCache !== 'undefined') ? _libTrackCache[AutoDJ._history[h]] : null;
            if (ht) {
                var hkey = ((ht.artist || '') + '|' + (ht.title || '')).toLowerCase().replace(/[^a-z0-9]/g, '');
                playedTitles[hkey] = true;
            }
        }

        var candidates = [];
        for (var i = 0; i < availableTracks.length; i++) {
            var t = availableTracks[i];
            if (t.id === currentTrackId) continue;
            if (AutoDJ._history.indexOf(t.id) >= 0) continue;
            // Skip duplicates by normalized title
            var tkey = ((t.artist || '') + '|' + (t.title || '')).toLowerCase().replace(/[^a-z0-9]/g, '');
            if (playedTitles[tkey]) continue;
            var score = 0;
            score += AutoDJ._scoreBPM(current ? current.bpm : 0, t.bpm);
            score += AutoDJ._scoreKey(current ? current.key : '', t.key);
            candidates.push({id: t.id, score: score});
        }

        if (!candidates.length) {
            // All tracks in history, reset
            AutoDJ._history = [currentTrackId];
            return AutoDJ.getNextTrack(currentTrackId, availableTracks);
        }

        candidates.sort(function(a, b) { return b.score - a.score; });

        // Pick randomly from top candidates (within 5 points of best)
        var bestScore = candidates[0].score;
        var topCandidates = candidates.filter(function(c) { return c.score >= bestScore - 5; });
        var chosen = topCandidates[Math.floor(Math.random() * topCandidates.length)];

        AutoDJ._history.push(chosen.id);
        var chosenTrack = availableTracks.find(function(t) { return t.id === chosen.id; });
        AutoDJ._log('getNextTrack: chose id=' + chosen.id + ' "' + (chosenTrack ? chosenTrack.title : '?') + '" score=' + chosen.score + ' candidates=' + candidates.length + ' history=' + AutoDJ._history.length);

        return chosen.id;
    },

    playNext: function() {
        AutoDJ._log('playNext called, currentId=' + (typeof _libPlayingTrackId !== 'undefined' ? _libPlayingTrackId : 'undef'));
        var currentId = (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : null;
        if (currentId && AutoDJ._history.indexOf(currentId) < 0) {
            AutoDJ._history.push(currentId);
        }
        var tracks = (typeof _libTracks !== 'undefined' && _libTracks && _libTracks.length) ? _libTracks : null;
        AutoDJ._log('playNext: tracks available=' + (tracks ? tracks.length : 0));

        if (tracks) {
            var nextId = AutoDJ.getNextTrack(currentId, tracks);
            AutoDJ._log('playNext: nextId=' + nextId);
            if (nextId && typeof playLibraryTrackById === 'function') {
                playLibraryTrackById(nextId);
                return;
            }
        }

        // Fallback: ask server
        AutoDJ.fetchNextFromServer(currentId, function(trackId) {
            if (trackId && typeof playLibraryTrackById === 'function') {
                playLibraryTrackById(trackId);
            } else if (typeof _playRandomTrack === 'function') {
                _playRandomTrack();
            }
        });
    },

    fetchNextFromServer: function(currentTrackId, callback) {
        var url = '/api/autodj/next?track_id=' + (currentTrackId || 0);
        var playlistId = (typeof _selectedPlaylistId !== 'undefined') ? _selectedPlaylistId : null;
        if (playlistId) url += '&playlist_id=' + playlistId;
        var stream = (typeof _currentFolder !== 'undefined' && _currentFolder) ? _currentFolder : '';
        if (stream) url += '&stream=' + encodeURIComponent(stream);
        fetch(url, {credentials: 'include'})
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data && data.id) {
                    AutoDJ._history.push(data.id);
                    callback(data.id);
                } else {
                    callback(null);
                }
            })
            .catch(function() { callback(null); });
    },

    // --- Crossfade with outro detection ---

    _detectOutroStart: function() {
        // Use waveform data to find where the track gets quiet (outro)
        // Returns seconds before end where crossfade should start
        if (typeof _waveformData === 'undefined' || !_waveformData || !_waveformData.length) {
            return AutoDJ._fadeDuration / 1000; // fallback: 10s
        }
        var bars = _waveformData.length;
        var dur = (_playerAudio && _playerAudio.duration && isFinite(_playerAudio.duration))
            ? _playerAudio.duration : (typeof _libTrackDuration !== 'undefined' ? _libTrackDuration : 0);
        if (!dur || dur <= 0) return AutoDJ._fadeDuration / 1000;

        // Scan from the end backwards — find where volume drops below 30% of track average
        var sum = 0;
        for (var i = 0; i < bars; i++) sum += _waveformData[i];
        var avg = sum / bars;
        var threshold = avg * 0.3;

        // Find the point where the track starts getting quiet near the end
        var outroBar = bars;
        var quietCount = 0;
        for (var i = bars - 1; i >= Math.floor(bars * 0.5); i--) {
            if (_waveformData[i] < threshold) {
                quietCount++;
                if (quietCount >= 3) { // at least 3 consecutive quiet bars
                    outroBar = i;
                }
            } else {
                quietCount = 0;
            }
        }

        var outroSec = dur - (outroBar / bars * dur);
        // Clamp between 5s and 15s
        outroSec = Math.max(5, Math.min(15, outroSec));
        return outroSec;
    },

    _checkFade: function() {
        if (!AutoDJ.enabled || AutoDJ._fading) return;
        if (!_playerAudio || !_isLibraryTrack) return;
        var dur = (_playerAudio.duration && isFinite(_playerAudio.duration))
            ? _playerAudio.duration : (typeof _libTrackDuration !== 'undefined' ? _libTrackDuration : 0);
        if (!dur || dur <= 0) return;

        var fadeStartSec = AutoDJ._detectOutroStart();
        var remaining = dur - _playerAudio.currentTime;
        if (remaining > fadeStartSec || remaining <= 0) return;

        // Start crossfade
        AutoDJ._log('_checkFade: STARTING crossfade, remaining=' + remaining.toFixed(1) + 's, fadeStart=' + fadeStartSec.toFixed(1) + 's');
        AutoDJ._fading = true;
        AutoDJ._crossfadeComplete = false;
        AutoDJ._originalVolume = (typeof _browserVolume !== 'undefined') ? _browserVolume : _playerAudio.volume;

        // Add current track to history
        var currentId = (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : null;
        if (currentId && AutoDJ._history.indexOf(currentId) < 0) {
            AutoDJ._history.push(currentId);
        }

        // Pick next track
        var tracks = (typeof _libTracks !== 'undefined' && _libTracks && _libTracks.length) ? _libTracks : null;
        var nextId = tracks ? AutoDJ.getNextTrack(currentId, tracks) : null;

        if (nextId) {
            AutoDJ._nextTrackId = nextId;
            // Create second audio element for the incoming track
            AutoDJ._log('Creating fadeAudio for track id=' + nextId);
            AutoDJ._fadeAudio = new Audio();
            AutoDJ._fadeAudio.crossOrigin = 'anonymous';
            AutoDJ._fadeAudio.src = '/api/library/track/' + nextId + '/play';
            AutoDJ._fadeAudio.volume = 0;
            AutoDJ._fadeAudio.addEventListener('error', function(e) { AutoDJ._log('fadeAudio ERROR event: ' + (e.target.error ? e.target.error.message : 'unknown')); });
            AutoDJ._fadeAudio.play().catch(function(e) { AutoDJ._log('fadeAudio play ERROR: ' + e.message); });

            // Crossfade: fade out current, fade in next
            var fadeDurationMs = remaining * 1000;
            var steps = fadeDurationMs / 50;
            var outStep = AutoDJ._originalVolume / steps;
            var inStep = AutoDJ._originalVolume / steps;

            AutoDJ._fadeInterval = setInterval(function() {
                if (!_playerAudio || !AutoDJ._fadeAudio) {
                    AutoDJ._resetFade();
                    return;
                }
                // Fade out current
                _playerAudio.volume = Math.max(0, _playerAudio.volume - outStep);
                // Fade in next
                AutoDJ._fadeAudio.volume = Math.min(AutoDJ._originalVolume, AutoDJ._fadeAudio.volume + inStep);

                if (_playerAudio.volume <= 0.01) {
                    AutoDJ._log('Fade complete (volume=0), calling _finishCrossfade');
                    clearInterval(AutoDJ._fadeInterval);
                    AutoDJ._fadeInterval = null;
                    AutoDJ._finishCrossfade();
                }
            }, 50);
        } else {
            // No next track — just fade out
            var steps = (remaining * 1000) / 50;
            var outStep = AutoDJ._originalVolume / steps;
            AutoDJ._fadeInterval = setInterval(function() {
                if (!_playerAudio) { AutoDJ._resetFade(); return; }
                _playerAudio.volume = Math.max(0, _playerAudio.volume - outStep);
                if (_playerAudio.volume <= 0.01) { AutoDJ._resetFade(); }
            }, 50);
        }
    },

    _finishCrossfade: function() {
        AutoDJ._log('_finishCrossfade: nextTrackId=' + AutoDJ._nextTrackId + ' fadeAudio=' + (AutoDJ._fadeAudio ? 'yes' : 'no') + ' _fading=' + AutoDJ._fading);
        if (!AutoDJ._nextTrackId) {
            AutoDJ._log('_finishCrossfade: no nextTrackId, aborting');
            AutoDJ._fading = false;
            return;
        }
        AutoDJ._crossfadeComplete = true;

        // Stop the fade interval immediately — critical: otherwise it keeps fading the NEW track
        if (AutoDJ._fadeInterval) {
            AutoDJ._log('_finishCrossfade: clearing fadeInterval');
            clearInterval(AutoDJ._fadeInterval);
            AutoDJ._fadeInterval = null;
        }
        var nextId = AutoDJ._nextTrackId;
        var fadeAudio = AutoDJ._fadeAudio;
        var oldAudio = _playerAudio;
        AutoDJ._fadeAudio = null;
        AutoDJ._nextTrackId = null;
        AutoDJ._fading = false;

        if (!nextId || !fadeAudio) return;

        // Kill old player completely
        if (oldAudio) {
            oldAudio.removeEventListener('timeupdate', _onSeekUpdate);
            oldAudio.removeEventListener('ended', _onPlayerEnded);
            oldAudio.removeEventListener('error', _onPlayerError);
            oldAudio.pause();
            oldAudio.src = '';
        }

        // Swap: fadeAudio becomes the main player (continues playing seamlessly)
        AutoDJ._log('Swapping audio: fadeAudio.currentTime=' + (fadeAudio ? fadeAudio.currentTime.toFixed(1) : '?') + ' paused=' + (fadeAudio ? fadeAudio.paused : '?'));
        fadeAudio.volume = AutoDJ._originalVolume || _browserVolume;
        AutoDJ._originalVolume = null;
        _playerAudio = fadeAudio;
        _playerAudio.addEventListener('timeupdate', _onSeekUpdate);
        _playerAudio.addEventListener('ended', _onPlayerEnded);
        _playerAudio.addEventListener('error', _onPlayerError);

        // Update player state
        _isLibraryTrack = true;
        _libPlayingTrackId = nextId;
        _browserPaused = false;
        var cached = (typeof _libTrackCache !== 'undefined') ? _libTrackCache[nextId] : null;
        if (cached) {
            _browserLibStream = cached.stream_subdir || '';
            _libTrackDuration = cached.duration_sec || 0;
            _browserIcyTrack = (cached.artist ? cached.artist + ' - ' : '') + (cached.title || '');
        }
        sessionStorage.setItem('_libPlayingTrackId', nextId);
        sessionStorage.setItem('_browserLibStream', _browserLibStream || '');

        // Load track-specific data
        if (typeof _loadWaveformById === 'function') _loadWaveformById(nextId);
        if (typeof _loadCuePoints === 'function') _loadCuePoints(nextId);
        if (typeof _loadLibraryCover === 'function' && cached) {
            _loadLibraryCover(nextId, cached.artist || '', cached.title || '');
        }

        // Force full player bar re-render
        AutoDJ._log('Updating UI: _libPlayingTrackId=' + _libPlayingTrackId + ' _browserIcyTrack=' + _browserIcyTrack);
        var preRefreshSrc = _playerAudio ? _playerAudio.src : '?';
        if (typeof _lastPlayerKey !== 'undefined') _lastPlayerKey = '';
        if (typeof updatePlayButtons === 'function') updatePlayButtons();
        AutoDJ._log('After updatePlayButtons: paused=' + (_playerAudio ? _playerAudio.paused : '?') + ' src changed=' + (_playerAudio && _playerAudio.src !== preRefreshSrc));
        if (typeof _refreshPlayerBar === 'function') _refreshPlayerBar();
        AutoDJ._log('After _refreshPlayerBar: paused=' + (_playerAudio ? _playerAudio.paused : '?') + ' vol=' + (_playerAudio ? _playerAudio.volume.toFixed(2) : '?'));

        // Re-render track list for gray played tracks
        if (typeof _vsRenderedStart !== 'undefined') {
            _vsRenderedStart = -1;
            if (typeof _vsRenderVisible === 'function') _vsRenderVisible();
        }

        // Monitor player state after swap — every 200ms for 3 seconds
        var _monCount = 0;
        var _monInterval = setInterval(function() {
            _monCount++;
            var p = _playerAudio;
            AutoDJ._log('MON ' + (_monCount * 200) + 'ms: paused=' + (p ? p.paused : '?') + ' src=' + (p ? (p.src ? p.src.substr(-30) : 'EMPTY') : '?') + ' vol=' + (p ? p.volume.toFixed(2) : '?') + ' time=' + (p ? p.currentTime.toFixed(1) : '?'));
            if (_monCount >= 15) {
                clearInterval(_monInterval);
                AutoDJ._crossfadeComplete = false;
            }
        }, 200);
    },

    _resetFade: function() {
        if (AutoDJ._fadeInterval) {
            clearInterval(AutoDJ._fadeInterval);
            AutoDJ._fadeInterval = null;
        }
        if (AutoDJ._fadeAudio) {
            AutoDJ._fadeAudio.pause();
            AutoDJ._fadeAudio = null;
        }
        AutoDJ._nextTrackId = null;
        AutoDJ._fading = false;
        if (AutoDJ._originalVolume !== null && typeof _playerAudio !== 'undefined' && _playerAudio) {
            _playerAudio.volume = AutoDJ._originalVolume;
        }
        AutoDJ._originalVolume = null;
    },

    // --- Scoring ---

    _scoreBPM: function(currentBpm, candidateBpm) {
        if (!currentBpm || !candidateBpm || currentBpm <= 0 || candidateBpm <= 0) return 25;
        var ratio = candidateBpm / currentBpm;
        // Check normal range (0.95 - 1.05)
        if (ratio >= 0.95 && ratio <= 1.05) {
            return 50 - Math.abs(1 - ratio) * 1000;
        }
        // Check half-time (0.475 - 0.525)
        if (ratio >= 0.475 && ratio <= 0.525) {
            return 35 - Math.abs(0.5 - ratio) * 700;
        }
        // Check double-time (1.9 - 2.1)
        if (ratio >= 1.9 && ratio <= 2.1) {
            return 35 - Math.abs(2 - ratio) * 350;
        }
        return 0;
    },

    _scoreKey: function(currentKey, candidateKey) {
        if (!currentKey || !candidateKey) return 25;
        var cam1 = (typeof _CAMELOT !== 'undefined') ? _CAMELOT[currentKey] : null;
        var cam2 = (typeof _CAMELOT !== 'undefined') ? _CAMELOT[candidateKey] : null;
        if (!cam1 || !cam2) return 25;

        var p1 = AutoDJ._parseCamelot(cam1);
        var p2 = AutoDJ._parseCamelot(cam2);
        if (!p1 || !p2) return 25;

        // Exact match
        if (p1.num === p2.num && p1.letter === p2.letter) return 50;
        // Same letter, adjacent number (with wraparound 12<->1)
        if (p1.letter === p2.letter) {
            var diff = Math.abs(p1.num - p2.num);
            if (diff === 1 || diff === 11) return 40;
        }
        // Same number, different letter (relative major/minor)
        if (p1.num === p2.num && p1.letter !== p2.letter) return 35;
        // 2 steps away same letter
        if (p1.letter === p2.letter) {
            var diff = Math.abs(p1.num - p2.num);
            if (diff === 2 || diff === 10) return 20;
        }
        return 0;
    },

    _parseCamelot: function(code) {
        if (!code) return null;
        var m = code.match(/^(\d+)([AB])$/);
        if (!m) return null;
        return {num: parseInt(m[1]), letter: m[2]};
    }
};

// Restore button state and load crossfade setting on page load
function _initAutoDJ() {
    AutoDJ._updateButton();
    fetch('/api/settings/autodj', {credentials: 'include'})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.crossfade_sec) {
                AutoDJ._fadeDuration = data.crossfade_sec * 1000;
                localStorage.setItem('_autoDJFade', data.crossfade_sec);
            }
        }).catch(function() {});
}
document.addEventListener('DOMContentLoaded', _initAutoDJ);
if (document.readyState !== 'loading') _initAutoDJ();
