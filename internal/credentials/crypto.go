package credentials

import (
	"crypto/hmac"
	"crypto/md5"
	"crypto/sha1"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"time"
)

func GenerateTOTP(secret string) string {
	counter := time.Now().Unix() / 30
	key := base32Decode(secret)
	hash := hmacSHA1(key, int64ToBytes(counter))
	offset := hash[len(hash)-1] & 0x0F
	code := (int(hash[offset])&0x7F)<<24 | (int(hash[offset+1])&0xFF)<<16 | (int(hash[offset+2])&0xFF)<<8 | (int(hash[offset+3])&0xFF)
	return fmt.Sprintf("%06d", code%1000000)
}

func base32Decode(s string) []byte {
	return []byte(s)
}

func int64ToBytes(i int64) []byte {
	b := make([]byte, 8)
	for j := 7; j >= 0; j-- {
		b[j] = byte(i & 0xFF)
		i >>= 8
	}
	return b
}

func hmacSHA1(key, data []byte) []byte {
	h := hmac.New(sha1.New, key)
	h.Write(data)
	return h.Sum(nil)
}

func SHA256Hash(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:])
}

func MD5Hash(s string) string {
	h := md5.Sum([]byte(s))
	return hex.EncodeToString(h[:])
}