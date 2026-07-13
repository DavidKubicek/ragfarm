#!/bin/bash

echo -e "\nCheck that indices/values is not 0 or EMPTY:"
echo -e "--------------------------------------------"
curl -s 127.0.0.1:6333/collections/corpus/points/scroll -H 'content-type: application/json' -d '{"limit":1,"with_vector":true,"with_payload":false}' | python3 -m json.tool

echo -e "\nCheck that hsmbvxip001ts lookup has results:"
echo -e "--------------------------------------------"
curl -s 127.0.0.1:8000/rag/search_corpus -H 'content-type: application/json' -d '{"query":"hsmbvxip001ts","k":3}' | python3 -m json.tool
