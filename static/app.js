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
                    if (s.running) {
                        statusCell.innerHTML = '<span class="status-running">running</span>';
                    } else {
                        statusCell.innerHTML = '<span class="status-stopped">stop</span>';
                    }
                }

                const track = row.querySelector('.track-cell');
                if (track) track.textContent = s.current_track || '-';

                const files = row.querySelector('.files-cell');
                if (files) {
                    var filesHtml = s.file_count;
                    if (s.yt_stats) {
                        filesHtml += '<small style="display:block;color:var(--pico-muted-color,#888);">' + s.yt_stats.downloaded + ' YT / ' + s.yt_stats.not_found + ' miss</small>';
                    }
                    files.innerHTML = filesHtml;
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

// Start polling
setInterval(updateStatus, 5000);
updateStatus();
