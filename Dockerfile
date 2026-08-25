# Stage 1: Build the Go binary
FROM golang:1.25-alpine AS builder

# Install gcc and musl-dev because github.com/mattn/go-sqlite3 requires CGO
RUN apk add --no-cache gcc musl-dev

# Set the working directory inside the container
WORKDIR /app

# Copy the Go modules manifests first for caching
COPY go.mod go.sum ./
RUN go mod download

# Copy the rest of the source code
COPY . .

# Build the application with CGO enabled (required for SQLite)
RUN CGO_ENABLED=1 GOOS=linux go build -o drmt_server ./cmd/drmt

# Stage 2: Create the minimal runtime container
FROM alpine:latest

# Add timezone data and CA certificates for secure web requests
RUN apk add --no-cache ca-certificates tzdata

# Set the working directory to /app
WORKDIR /app

# Copy the compiled binary from the builder stage
COPY --from=builder /app/drmt_server /app/drmt_server

# Expose the web server port
EXPOSE 3100

# Command to run the executable
CMD ["/app/drmt_server"]
