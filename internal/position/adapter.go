package position

import (
	"context"

	"drmt/internal/models"
)

// BrokerAdapter represents a client that fetches positions and streams them to the feed.
type BrokerAdapter interface {
	// Start connects to the broker API/WebSocket and pumps data into the feed.
	// It should handle its own reconnection logic if it is WebSocket-based, 
	// or its own polling loop if it is HTTP-based.
	Start(ctx context.Context, feed chan<- models.Position)
}
