package logger

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"time"
	"sync"
)

type LogLevel string

const (
	INFO  LogLevel = "INFO"
	ERROR LogLevel = "ERROR"
	WARN  LogLevel = "WARN"
)

// ServiceLogger handles hourly rotating file logs and console output
type LogEntry struct {
	ID        int64  `json:"id"`
	Timestamp string `json:"timestamp"`
	Level     string `json:"level"`
	Message   string `json:"message"`
	Module    string `json:"module"`
}

type ServiceLogger struct {
	ServiceName string
	mu          sync.RWMutex
	history     []LogEntry
}


func (l *ServiceLogger) GetHistory() []LogEntry {
	l.mu.RLock()
	defer l.mu.RUnlock()
	copy := make([]LogEntry, len(l.history))
	for i, entry := range l.history {
		copy[i] = entry
	}
	return copy
}

func NewLogger(serviceName string) *ServiceLogger {
	return &ServiceLogger{ServiceName: serviceName}
}

// Global Logger Instances mapped to your requested folder structure
var (
	Auth    = NewLogger("drmt.auth")
	Exit    = NewLogger("drmt.exit_order_flow")
	Monitor = NewLogger("drmt.order_monitoring")
	System  = NewLogger("drmt.system_startup")
	Health  = NewLogger("drmt.health_monitor")
)

func (l *ServiceLogger) Info(format string, v ...interface{}) {
	l.writeLog(INFO, format, v...)
}

func (l *ServiceLogger) Error(format string, v ...interface{}) {
	l.writeLog(ERROR, format, v...)
}

func (l *ServiceLogger) Warn(format string, v ...interface{}) {
	l.writeLog(WARN, format, v...)
}

func (l *ServiceLogger) writeLog(level LogLevel, format string, v ...interface{}) {
	now := time.Now()

	// Format: YYYY-MM-DD HH:MM:SS
	timeStr := now.Format("2006-01-02 15:04:05")

	// Get Caller Module & Line (skip 2 stack frames to get the actual caller)
	_, file, line, ok := runtime.Caller(2)
	module := "unknown"
	if ok {
		module = filepath.Base(file)
	}

	// Format the message
	message := fmt.Sprintf(format, v...)
	if len(message) > 0 && message[len(message)-1] == '\n' {
		message = message[:len(message)-1] // trim trailing newline if present
	}

	// Python-style format: 2026-08-25 15:30:00 | INFO     | engine.go:123 | Message
	logLine := fmt.Sprintf("%s | %-8s | %s:%d | %s\n", timeStr, level, module, line, message)

	// 0. Save to in-memory ring buffer (last 100)
	l.mu.Lock()
	l.history = append([]LogEntry{{
		ID:        now.UnixNano(),
		Timestamp: timeStr,
		Level:     string(level),
		Message:   message,
		Module:    fmt.Sprintf("%s:%d", module, line),
	}}, l.history...)
	if len(l.history) > 100 {
		l.history = l.history[:100]
	}
	l.mu.Unlock()

	// 1. Output to Console (Stdout) just like the Python StreamHandler
	os.Stdout.WriteString(logLine)

	// 2. Output to Hourly Rotating File
	dir := filepath.Join("data", "logs", l.ServiceName)
	os.MkdirAll(dir, 0755)

	// Prefix: 'app' or 'error'
	prefix := "app"
	if level == ERROR {
		prefix = "error"
	}

	// Suffix: _YYYY-MM-DD_HH.log
	filename := fmt.Sprintf("%s_%s.log", prefix, now.Format("2006-01-02_15"))
	filePath := filepath.Join(dir, filename)

	f, err := os.OpenFile(filePath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0666)
	if err == nil {
		f.WriteString(logLine)
		f.Close()
	}
}
