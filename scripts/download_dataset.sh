#!/usr/bin/env bash
# Download the Sparkov card-transaction dataset and verify it byte-for-byte.
#
# Source of record: Kaggle "Credit Card Transactions Fraud Detection Dataset" (kartik2112),
# generated with the MIT-licensed Sparkov generator. Kaggle requires login, so by default this
# fetches an unofficial Hugging Face mirror and pins its SHA-256. The mirror was verified against
# the Kaggle original's published row counts, fraud counts and time ranges (docs/data/).
# If you download fraudTrain.csv / fraudTest.csv from Kaggle yourself, place them in
# ml/data/raw/sparkov/ and pass those paths to `make ingest RAW=...` instead.
set -euo pipefail

DEST="${1:-ml/data/raw/sparkov}"
BASE="https://huggingface.co/datasets/pointe77/credit-card-transaction/resolve/main"
# Portable to bash 3.2 (macOS default): "<file> <sha256>" pairs.
FILES="credit_card_transaction_train.csv 1a100d7d1e5e77442a5ce632293657c099d70ea0991797e92b55eb987da59f9f
credit_card_transaction_test.csv 05fbcc2d99d2463d189a916532c3db4179f21dbe8e5aa8810699a45cee60574f"

mkdir -p "$DEST"
echo "$FILES" | while read -r f sha; do
  if [[ ! -f "$DEST/$f" ]]; then
    echo "downloading $f"
    curl --fail --location --silent --show-error -o "$DEST/$f.part" "$BASE/$f"
    mv "$DEST/$f.part" "$DEST/$f"
  fi
  echo "$sha  $DEST/$f" | shasum -a 256 -c -
done
