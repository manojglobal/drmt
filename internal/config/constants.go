package config

const (
	BrokerMStock     = "MSTOCK"
	BrokerAliceBlue  = "ALICE BLUE"
	BrokerFyers      = "FYERS"
	BrokerFlattrade  = "FLATTRADE"
	BrokerDhan       = "DHAN"

	DropboxAppKey       = "DRMT_DROPBOX_APP_KEY"
	DropboxAppSecret    = "DRMT_DROPBOX_APP_SECRET"
	DropboxRefreshToken = "DRMT_DROPBOX_REFRESH_TOKEN"
	DefaultProxyURL     = "https://demoapi.quantivo.in/proxy"

	CredentialsFileName = "plato_client_credentials.csv"
	DropboxFilePath     = "/" + CredentialsFileName
	LocalCredentialsDir = "static"
	LocalCredentialsPath = LocalCredentialsDir + "/" + CredentialsFileName

	MaxCredentialSetsPerClient = 4
	MaxTokenGenRetries         = 3
	TokenGenRetryDelaySeconds  = 1

	AliceBlueBaseURL       = "https://ant.aliceblueonline.com"
	AliceBlueVerifyUser    = "/omk/auth/access/verify/user"
	AliceBlueGetEncKey     = "/omk/auth/access/client/enckey"
	AliceBlueValidatePwd   = "/omk/auth/access/v2/pwd/validate"
	AliceBlueVerifyTOTP    = "/omk/auth/access/topt/verify"
	AliceBlueSSOAuthorize  = "/omk/auth/sso/vendor/authorize/check"
	AliceBlueGetUserDetails = "/open-api/od/v1/vendor/getUserDetails"

	FlattradeLoginURL      = "https://emergeapi.flattrade.in/quickAuth"
	FlattradeSource        = "WEB"
	FlattradeDevice        = "Chrome"
	FlattradeAddlDivInfo   = "Linux"
	FlattradeUserAgent     = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
	FlattradeOrigin        = "https://emerge.flattrade.in"
	FlattradeReferer       = "https://emerge.flattrade.in/"

	MStockLoginURL         = "https://api.mstock.trade/openapi/typeb/session/verifytotp"
	MStockMiraeVersion     = "1"

	HTTPTimeoutSeconds     = 20
	HTTPMaxRetries         = 5
	HTTPRetryBaseDelayMs   = 1000

	KolkatataTimezone      = "Asia/Kolkata"
)

var ServersToCheck = map[string]string{
	"test_server": "https://testpts.quantivo.in/api/health",
	"demo_server": "https://demopts.quantivo.in/api/health",
}