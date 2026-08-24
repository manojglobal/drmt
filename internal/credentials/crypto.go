package credentials

import (
	"crypto/md5"
	"crypto/sha256"
	"encoding/hex"
	"log"
	"time"

	"github.com/pquerna/otp/totp"
)

func GenerateTOTP(secret string) string {
	otpStr, err := totp.GenerateCode(secret, time.Now())
	if err != nil {
		log.Printf("Failed to generate TOTP: %v", err)
		return "000000"
	}
	return otpStr
}

func SHA256Hash(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:])
}

func MD5Hash(s string) string {
	h := md5.Sum([]byte(s))
	return hex.EncodeToString(h[:])
}