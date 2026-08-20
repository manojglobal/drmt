package drmt

import "embed"

//go:embed web/* migrations/*
var AssetsFS embed.FS
