package inference

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/0frost0/research-assistant-zotero/backend/internal/store"
)

func TestCancellationReachesWorker(t *testing.T) {
	started, stopped := make(chan struct{}), make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer private" {
			t.Error("missing worker authentication")
		}
		// Consume request body before waiting so net/http can detect disconnects.
		var buf [4096]byte
		r.Body.Read(buf[:])
		close(started)
		<-r.Context().Done()
		close(stopped)
	}))
	defer server.Close()
	client := New(server.URL, "private")
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, err := client.Call(ctx, "fixture", "model", store.Object{"payload": store.Object{}})
		done <- err
	}()
	<-started
	cancel()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("cancelled call succeeded")
		}
	case <-time.After(time.Second):
		t.Fatal("client did not cancel")
	}
	select {
	case <-stopped:
	case <-time.After(time.Second):
		t.Fatal("worker did not observe disconnect")
	}
}
