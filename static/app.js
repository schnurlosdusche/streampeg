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
                            if (s.yt_stats.dl_yt) sub += ' + ' + s.yt_stats.dl_yt + ' YT';
                        } else {
                            sub = s.yt_stats.dl_yt + ' YT';
                            if (s.yt_stats.dl_sc) sub += ' + ' + s.yt_stats.dl_sc + ' SC';
                        }
                        sub += ' / ' + s.yt_stats.songs_seen + ' gehört';
                        trackHtml += '<small style="display:block;color:var(--pico-muted-color,#888);">' + sub + '</small>';
                    } else if (s.track_stats && (s.track_stats.recorded + s.track_stats.skipped) > 0) {
                        trackHtml += '<small style="display:block;color:var(--pico-muted-color,#888);">' + s.track_stats.recorded + ' rec / ' + (s.track_stats.recorded + s.track_stats.skipped) + ' gehört</small>';
                    }
                    track.innerHTML = trackHtml;
                }

                const files = row.querySelector('.files-cell');
                if (files) {
                    files.textContent = s.file_count;
                }

                const recPct = row.querySelector('.rec-pct-cell');
                if (recPct) {
                    recPct.textContent = s.rec_pct !== undefined ? s.rec_pct : '-';
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

// --- Inline stream player ---
var _playerAudio = null;
var _playerStreamId = null;

function toggleListen(streamId, url) {
    if (!_playerAudio) {
        _playerAudio = new Audio();
        _playerAudio.addEventListener('error', function() { stopListen(); });
    }

    if (_playerStreamId === streamId) {
        // Same stream: stop
        stopListen();
        return;
    }

    // Stop previous if any
    _playerAudio.pause();
    _playerAudio.removeAttribute('src');
    _playerAudio.load();
    _updateListenIcons(null);

    // Play new
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

// Start polling
setInterval(updateStatus, 5000);
updateStatus();
