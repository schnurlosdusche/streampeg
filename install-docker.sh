#!/bin/bash
# streampeg Docker Installer
# Usage: curl -sL http://YOUR_SERVER/install.sh | bash
set -e

echo "=== streampeg Docker Installer ==="
echo ""

# Create directory
INSTALL_DIR="${1:-./streampeg}"
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/recording" "$INSTALL_DIR/library"
cd "$INSTALL_DIR"

# Download image if not already loaded
if ! docker images --format '{{.Repository}}:{{.Tag}}' | grep -q '^streampeg:latest$'; then
    echo "Downloading streampeg Docker image..."
    TMPFILE=$(mktemp)
    curl -L -o "$TMPFILE" "http://192.168.1.57:3000/martin/streampeg/releases/download/v0.0.171a/streampeg-docker.tar"
    echo "Loading image into Docker..."
    docker load -i "$TMPFILE"
    docker tag 192.168.1.57:3000/martin/streampeg:latest streampeg:latest
    rm -f "$TMPFILE"
    echo "Image loaded."
else
    echo "Image already loaded."
fi

# Create docker-compose.yml
cat > docker-compose.yml << 'YAML'
services:
  streampeg:
    image: streampeg:latest
    container_name: streampeg
    restart: unless-stopped
    ports:
      - "5001:5000"
      - "3483:3483"
      - "9000:9000"
      - "9090:9090"
      - "9091:9091"
    volumes:
      - ./data:/data
      - ./recording:/recording
      - ./library:/library
YAML

# Start
echo "Starting streampeg..."
docker compose up -d

echo ""
echo "=== streampeg is running ==="
echo "Open http://localhost:5001"
echo ""
echo "Commands:"
echo "  docker compose logs -f    # View logs"
echo "  docker compose stop       # Stop"
echo "  docker compose start      # Start"
echo "  docker compose down       # Remove container"
