#!/bin/sh

DIR="runtime/jobs"
DAYS=30
DELETE=0

if [ "$1" = "--delete" ]; then
  DELETE=1
fi

if [ $DELETE -eq 1 ]; then
  echo "Deleting directories in $DIR older than $DAYS days..."
  find "$DIR" -mindepth 1 -maxdepth 1 -type d -mtime +$DAYS -print -exec rm -rf {} \;
else
  echo "Dry-run: directories in $DIR older than $DAYS days:"
  find "$DIR" -mindepth 1 -maxdepth 1 -type d -mtime +$DAYS -print
fi