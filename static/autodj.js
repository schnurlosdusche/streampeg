/**
 * Auto-DJ Module for Streampeg
 * Smart track selection based on BPM compatibility and Key harmony (Camelot wheel).
 * Stufe 1: Smart queue + simple volume crossfade. No beatmatching.
 */

var AutoDJ = {
    enabled: localStorage.getItem('_autoDJEnabled') === '1',
    _history: [],
    _historyMax: 10,
    _fadeDuration: 3000,
    _fading: false,
    _originalVolume: null,
    _fadeInterval: null,

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
        }
        if (typeof _refreshPlayerBar === 'function') _refreshPlayerBar();
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

        var candidates = [];
        for (var i = 0; i < availableTracks.length; i++) {
            var t = availableTracks[i];
            if (t.id === currentTrackId) continue;
            if (AutoDJ._history.indexOf(t.id) >= 0) continue;
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
        if (AutoDJ._history.length > AutoDJ._historyMax) {
            AutoDJ._history.shift();
        }

        return chosen.id;
    },

    playNext: function() {
        var currentId = (typeof _libPlayingTrackId !== 'undefined') ? _libPlayingTrackId : null;
        var tracks = (typeof _libTracks !== 'undefined' && _libTracks && _libTracks.length) ? _libTracks : null;

        if (tracks) {
            var nextId = AutoDJ.getNextTrack(currentId, tracks);
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
                    if (AutoDJ._history.length > AutoDJ._historyMax) AutoDJ._history.shift();
                    callback(data.id);
                } else {
                    callback(null);
                }
            })
            .catch(function() { callback(null); });
    },

    // --- Crossfade ---

    _checkFade: function() {
        if (!AutoDJ.enabled || !AutoDJ._fading === false) return;
        if (AutoDJ._fading) return;
        if (!_playerAudio || !_isLibraryTrack) return;
        var dur = (_playerAudio.duration && isFinite(_playerAudio.duration)) ? _playerAudio.duration : (typeof _libTrackDuration !== 'undefined' ? _libTrackDuration : 0);
        if (!dur || dur <= 0) return;
        var remaining = (dur - _playerAudio.currentTime) * 1000;
        if (remaining > AutoDJ._fadeDuration || remaining <= 0) return;

        AutoDJ._fading = true;
        AutoDJ._originalVolume = _playerAudio.volume;
        var steps = AutoDJ._fadeDuration / 50;
        var volStep = AutoDJ._originalVolume / steps;
        AutoDJ._fadeInterval = setInterval(function() {
            if (!_playerAudio) {
                AutoDJ._resetFade();
                return;
            }
            _playerAudio.volume = Math.max(0, _playerAudio.volume - volStep);
            if (_playerAudio.volume <= 0.01) {
                AutoDJ._resetFade();
            }
        }, 50);
    },

    _resetFade: function() {
        if (AutoDJ._fadeInterval) {
            clearInterval(AutoDJ._fadeInterval);
            AutoDJ._fadeInterval = null;
        }
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
