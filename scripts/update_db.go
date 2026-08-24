package main

import (
	"log"
	"github.com/jmoiron/sqlx"
	_ "github.com/mattn/go-sqlite3"
)

func main() {
	db, err := sqlx.Connect("sqlite3", "/home/pratim/drmt_v2/drmt.db")
	if err != nil {
		log.Fatal(err)
	}
	defer db.Close()

	res, err := db.Exec("UPDATE credentials SET password = ? WHERE client_id = ?", "Manoj@123", "807663")
	if err != nil {
		log.Fatal(err)
	}
	rows, _ := res.RowsAffected()
	log.Printf("Updated password for %d row(s)", rows)
}
