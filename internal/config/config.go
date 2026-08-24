package config

import (
	"os"
	"path/filepath"
	"strings"
)

type Config struct {
	DropboxAppKey       string
	DropboxAppSecret    string
	DropboxRefreshToken string
	ProxyURL            string
	StaticIP            string
}

func Load() *Config {
	loadDotEnv()

	return &Config{
		DropboxAppKey:       getEnv(DropboxAppKey, "7yn83e37zk7108e"),
		DropboxAppSecret:    getEnv(DropboxAppSecret, "yneynsmqfcsudzo"),
		DropboxRefreshToken: getEnv(DropboxRefreshToken, "RzxxlOoW8wsAAAAAAAAAAVEUsAsPaTo7e-2fA2UYN95IOlCh3zL6Gg28BATK79Su"),
		ProxyURL:            getEnv("DRMT_PROXY_URL", DefaultProxyURL),
		StaticIP:            getEnv("DRMT_STATIC_IP", ""),
	}
}

func loadDotEnv() {
	execPath, err := os.Executable()
	if err != nil {
		return
	}
	dir := filepath.Dir(execPath)

	paths := []string{
		filepath.Join(dir, ".env"),
		filepath.Join(dir, "..", ".env"),
		"/opt/drmt/backend/.env",
		".env",
	}

	for _, p := range paths {
		absPath, _ := filepath.Abs(p)
		if _, err := os.Stat(absPath); err == nil {
			loadEnvFile(absPath)
			return
		}
	}
}

func loadEnvFile(path string) {
	content, err := os.ReadFile(path)
	if err != nil {
		return
	}

	lines := strings.Split(string(content), "\n")
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		parts := strings.SplitN(line, "=", 2)
		if len(parts) != 2 {
			continue
		}
		key := strings.TrimSpace(parts[0])
		value := strings.TrimSpace(parts[1])
		value = strings.Trim(value, `"`)
		value = strings.Trim(value, `'`)
		os.Setenv(key, value)
	}
}

func getEnv(key, defaultValue string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return defaultValue
}