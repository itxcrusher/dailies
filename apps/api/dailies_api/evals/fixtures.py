"""Real telemetry, captured from the live Grafana stack on 2026-08-31.

Captured rather than written. A fixture an author invents encodes what the author believes
the system emits, so an eval built on one measures the agent against that belief instead of
against the farm. These came off the Prometheus and Loki datasources for shots that
actually rendered: SH201 with a texture deliberately missing, SH200 clean.

SH200 has **no log lines at all**, and that is the fixture doing its job. A healthy render
is silent, and this project's recurring failure is reading silence as a broken query. An
eval whose clean case still had logs in it would never test that.

Held as JSON text and parsed at import, which is the third shape this file took. Written
with json.dumps it was not valid Python and raised NameError on `true`; written with
pprint it wrapped long log lines into implicit string concatenation, which is a silent way
to corrupt a fixture and which ruff refused. JSON in a raw string is neither: it is exactly
the bytes the datasource returned.

Range points are trimmed to the last three per series; nothing else is edited.
"""

from __future__ import annotations

import json
from typing import Any

SH201_EXPECTED: dict[str, Any] = json.loads(
    r"""{
 "status": "success",
 "data": {
  "resultType": "matrix",
  "result": [
   {
    "metric": {
     "__name__": "render_job_frames_expected",
     "instance": "1474c8d8-810f-4752-b06c-e61713f41927",
     "job": "dailies-render",
     "priority": "normal",
     "project": "dailies",
     "render_job": "vqa-bad",
     "renderer": "cycles",
     "scene": "Scene",
     "sequence": "SEQ01",
     "service_instance_id": "1474c8d8-810f-4752-b06c-e61713f41927",
     "service_name": "dailies-render",
     "shot": "SH201"
    },
    "values": [
     [
      1788110542,
      "1"
     ]
    ]
   },
   {
    "metric": {
     "__name__": "render_job_frames_expected",
     "instance": "e57a6043-aae3-45d7-b595-7e557723b8b2",
     "job": "dailies-render",
     "priority": "normal",
     "project": "dailies",
     "render_job": "vqa-bad",
     "renderer": "cycles",
     "scene": "Scene",
     "sequence": "SEQ01",
     "service_instance_id": "e57a6043-aae3-45d7-b595-7e557723b8b2",
     "service_name": "dailies-render",
     "shot": "SH201"
    },
    "values": [
     [
      1788108742,
      "1"
     ]
    ]
   }
  ]
 }
}"""
)

SH201_COMPLETED: dict[str, Any] = json.loads(
    r"""{
 "status": "success",
 "data": {
  "resultType": "matrix",
  "result": [
   {
    "metric": {
     "__name__": "render_job_frames_completed_total",
     "instance": "1474c8d8-810f-4752-b06c-e61713f41927",
     "job": "dailies-render",
     "priority": "normal",
     "project": "dailies",
     "render_job": "vqa-bad",
     "renderer": "cycles",
     "scene": "Scene",
     "sequence": "SEQ01",
     "service_instance_id": "1474c8d8-810f-4752-b06c-e61713f41927",
     "service_name": "dailies-render",
     "shot": "SH201"
    },
    "values": [
     [
      1788110542,
      "1"
     ]
    ]
   },
   {
    "metric": {
     "__name__": "render_job_frames_completed_total",
     "instance": "e57a6043-aae3-45d7-b595-7e557723b8b2",
     "job": "dailies-render",
     "priority": "normal",
     "project": "dailies",
     "render_job": "vqa-bad",
     "renderer": "cycles",
     "scene": "Scene",
     "sequence": "SEQ01",
     "service_instance_id": "e57a6043-aae3-45d7-b595-7e557723b8b2",
     "service_name": "dailies-render",
     "shot": "SH201"
    },
    "values": [
     [
      1788108742,
      "1"
     ]
    ]
   }
  ]
 }
}"""
)

SH201_LOGS: dict[str, Any] = json.loads(
    r"""{
 "status": "success",
 "data": {
  "resultType": "streams",
  "result": [
   {
    "stream": {
     "code_file_path": "/usr/local/lib/python3.11/site-packages/dailies_telemetry/log_emitter.py",
     "code_function_name": "record",
     "code_line_number": "104",
     "detected_level": "warn",
     "event_kind": "asset_missing",
     "observed_timestamp": "1788108705039879028",
     "project": "dailies",
     "render_job": "vqa-bad",
     "scope_name": "dailies.render.events.7fd17127a9d0",
     "sequence": "SEQ01",
     "service_instance_id": "e57a6043-aae3-45d7-b595-7e557723b8b2",
     "service_name": "dailies-render",
     "severity_number": "13",
     "severity_text": "WARN",
     "shot": "SH201",
     "telemetry_sdk_language": "python",
     "telemetry_sdk_name": "opentelemetry",
     "telemetry_sdk_version": "1.44.0",
     "worker": "dailies-render-5djnq"
    },
    "values": [
     [
      "1788108705039767040",
      "Warning: Unable to open file '/assets/jacket_diffuse.exr'"
     ]
    ]
   },
   {
    "stream": {
     "code_file_path": "/usr/local/lib/python3.11/site-packages/dailies_telemetry/log_emitter.py",
     "code_function_name": "record",
     "code_line_number": "104",
     "detected_level": "warn",
     "event_kind": "asset_missing",
     "observed_timestamp": "1788110306109646580",
     "project": "dailies",
     "render_job": "vqa-bad",
     "scope_name": "dailies.render.events.7fad50a86b10",
     "sequence": "SEQ01",
     "service_instance_id": "1474c8d8-810f-4752-b06c-e61713f41927",
     "service_name": "dailies-render",
     "severity_number": "13",
     "severity_text": "WARN",
     "shot": "SH201",
     "telemetry_sdk_language": "python",
     "telemetry_sdk_name": "opentelemetry",
     "telemetry_sdk_version": "1.44.0",
     "worker": "dailies-render-vsbv2"
    },
    "values": [
     [
      "1788110306109534976",
      "Warning: Unable to open file '/assets/jacket_diffuse.exr'"
     ]
    ]
   }
  ],
  "stats": {
   "summary": {
    "bytesProcessedPerSecond": 13455,
    "linesProcessedPerSecond": 31,
    "totalBytesProcessed": 1281,
    "totalLinesProcessed": 3,
    "execTime": 0.095202,
    "queueTime": 0.072169,
    "subqueries": 0,
    "totalEntriesReturned": 2,
    "splits": 13,
    "shards": 11,
    "totalPostFilterLines": 2,
    "totalStructuredMetadataBytesProcessed": 1050,
    "estimatedQueryBytes": 3072
   },
   "querier": {
    "store": {
     "totalChunksRef": 3,
     "totalChunksDownloaded": 3,
     "chunksDownloadTime": 150323498,
     "queryReferencedStructuredMetadata": true,
     "queryUsedV2Engine": false,
     "chunk": {
      "headChunkBytes": 0,
      "headChunkLines": 0,
      "decompressedBytes": 1281,
      "decompressedLines": 3,
      "compressedBytes": 363,
      "totalDuplicates": 0,
      "postFilterLines": 2,
      "headChunkStructuredMetadataBytes": 0,
      "decompressedStructuredMetadataBytes": 1050
     },
     "chunkRefsFetchTime": 13644073,
     "congestionControlLatency": 0,
     "pipelineWrapperFilteredLines": 0,
     "dataobj": {
      "prePredicateDecompressedRows": 0,
      "prePredicateDecompressedBytes": 0,
      "prePredicateDecompressedStructuredMetadataBytes": 0,
      "postPredicateRows": 0,
      "postPredicateDecompressedBytes": 0,
      "postPredicateStructuredMetadataBytes": 0,
      "postFilterRows": 0,
      "pagesScanned": 0,
      "pagesDownloaded": 0,
      "pagesDownloadedBytes": 0,
      "pageBatches": 0,
      "totalRowsAvailable": 0,
      "totalPageDownloadTime": 0,
      "wireBytesTransferred": 0
     }
    },
    "querierExecTime": 0.424305
   },
   "ingester": {
    "totalReached": 150,
    "totalChunksMatched": 0,
    "totalBatches": 150,
    "totalLinesSent": 0,
    "store": {
     "totalChunksRef": 0,
     "totalChunksDownloaded": 0,
     "chunksDownloadTime": 0,
     "queryReferencedStructuredMetadata": false,
     "queryUsedV2Engine": false,
     "chunk": {
      "headChunkBytes": 0,
      "headChunkLines": 0,
      "decompressedBytes": 0,
      "decompressedLines": 0,
      "compressedBytes": 0,
      "totalDuplicates": 0,
      "postFilterLines": 0,
      "headChunkStructuredMetadataBytes": 0,
      "decompressedStructuredMetadataBytes": 0
     },
     "chunkRefsFetchTime": 0,
     "congestionControlLatency": 0,
     "pipelineWrapperFilteredLines": 0,
     "dataobj": {
      "prePredicateDecompressedRows": 0,
      "prePredicateDecompressedBytes": 0,
      "prePredicateDecompressedStructuredMetadataBytes": 0,
      "postPredicateRows": 0,
      "postPredicateDecompressedBytes": 0,
      "postPredicateStructuredMetadataBytes": 0,
      "postFilterRows": 0,
      "pagesScanned": 0,
      "pagesDownloaded": 0,
      "pagesDownloadedBytes": 0,
      "pageBatches": 0,
      "totalRowsAvailable": 0,
      "totalPageDownloadTime": 0,
      "wireBytesTransferred": 0
     }
    },
    "recvWaitTime": 0.000747
   },
   "cache": {
    "chunk": {
     "entriesFound": 0,
     "entriesRequested": 3,
     "entriesStored": 6,
     "bytesReceived": 0,
     "bytesSent": 2099,
     "requests": 4,
     "downloadTime": 19892,
     "queryLengthServed": 0
    },
    "index": {
     "entriesFound": 0,
     "entriesRequested": 0,
     "entriesStored": 0,
     "bytesReceived": 0,
     "bytesSent": 0,
     "requests": 0,
     "downloadTime": 0,
     "queryLengthServed": 0
    },
    "result": {
     "entriesFound": 0,
     "entriesRequested": 12,
     "entriesStored": 11,
     "bytesReceived": 0,
     "bytesSent": 0,
     "requests": 23,
     "downloadTime": 6376735,
     "queryLengthServed": 0
    },
    "statsResult": {
     "entriesFound": 12,
     "entriesRequested": 12,
     "entriesStored": 7,
     "bytesReceived": 3384,
     "bytesSent": 0,
     "requests": 19,
     "downloadTime": 5886841,
     "queryLengthServed": 26167000000000
    },
    "volumeResult": {
     "entriesFound": 0,
     "entriesRequested": 0,
     "entriesStored": 0,
     "bytesReceived": 0,
     "bytesSent": 0,
     "requests": 0,
     "downloadTime": 0,
     "queryLengthServed": 0
    },
    "seriesResult": {
     "entriesFound": 0,
     "entriesRequested": 0,
     "entriesStored": 0,
     "bytesReceived": 0,
     "bytesSent": 0,
     "requests": 0,
     "downloadTime": 0,
     "queryLengthServed": 0
    },
    "labelResult": {
     "entriesFound": 0,
     "entriesRequested": 0,
     "entriesStored": 0,
     "bytesReceived": 0,
     "bytesSent": 0,
     "requests": 0,
     "downloadTime": 0,
     "queryLengthServed": 0
    },
    "instantMetricResult": {
     "entriesFound": 0,
     "entriesRequested": 0,
     "entriesStored": 0,
     "bytesReceived": 0,
     "bytesSent": 0,
     "requests": 0,
     "downloadTime": 0,
     "queryLengthServed": 0
    },
    "logResult": {
     "entriesFound": 0,
     "entriesRequested": 0,
     "entriesStored": 0,
     "bytesReceived": 0,
     "bytesSent": 0,
     "requests": 0,
     "downloadTime": 0,
     "queryLengthServed": 0
    },
    "taskResult": {
     "entriesFound": 0,
     "entriesRequested": 0,
     "entriesStored": 0,
     "bytesReceived": 0,
     "bytesSent": 0,
     "requests": 0,
     "downloadTime": 0,
     "queryLengthServed": 0
    }
   },
   "index": {
    "totalChunks": 3,
    "postFilterChunks": 3,
    "shardsDuration": 0,
    "usedBloomFilters": false,
    "totalStreams": 3,
    "chunkRefsLookupTime": 0.00184,
    "bloomFilterTime": 2.4e-05
   }
  }
 }
}"""
)

SH200_EXPECTED: dict[str, Any] = json.loads(
    r"""{
 "status": "success",
 "data": {
  "resultType": "matrix",
  "result": [
   {
    "metric": {
     "__name__": "render_job_frames_expected",
     "instance": "6597183a-8974-4a04-82d7-a72468272759",
     "job": "dailies-render",
     "priority": "normal",
     "project": "dailies",
     "render_job": "vqa-good",
     "renderer": "cycles",
     "scene": "Scene",
     "sequence": "SEQ01",
     "service_instance_id": "6597183a-8974-4a04-82d7-a72468272759",
     "service_name": "dailies-render",
     "shot": "SH200"
    },
    "values": [
     [
      1788110242,
      "1"
     ]
    ]
   },
   {
    "metric": {
     "__name__": "render_job_frames_expected",
     "instance": "e4324ce4-da5b-4022-8e8e-98322fae0ca1",
     "job": "dailies-render",
     "priority": "normal",
     "project": "dailies",
     "render_job": "vqa-good",
     "renderer": "cycles",
     "scene": "Scene",
     "sequence": "SEQ01",
     "service_instance_id": "e4324ce4-da5b-4022-8e8e-98322fae0ca1",
     "service_name": "dailies-render",
     "shot": "SH200"
    },
    "values": [
     [
      1788108742,
      "1"
     ]
    ]
   }
  ]
 }
}"""
)

SH200_COMPLETED: dict[str, Any] = json.loads(
    r"""{
 "status": "success",
 "data": {
  "resultType": "matrix",
  "result": [
   {
    "metric": {
     "__name__": "render_job_frames_completed_total",
     "instance": "6597183a-8974-4a04-82d7-a72468272759",
     "job": "dailies-render",
     "priority": "normal",
     "project": "dailies",
     "render_job": "vqa-good",
     "renderer": "cycles",
     "scene": "Scene",
     "sequence": "SEQ01",
     "service_instance_id": "6597183a-8974-4a04-82d7-a72468272759",
     "service_name": "dailies-render",
     "shot": "SH200"
    },
    "values": [
     [
      1788110242,
      "1"
     ]
    ]
   },
   {
    "metric": {
     "__name__": "render_job_frames_completed_total",
     "instance": "e4324ce4-da5b-4022-8e8e-98322fae0ca1",
     "job": "dailies-render",
     "priority": "normal",
     "project": "dailies",
     "render_job": "vqa-good",
     "renderer": "cycles",
     "scene": "Scene",
     "sequence": "SEQ01",
     "service_instance_id": "e4324ce4-da5b-4022-8e8e-98322fae0ca1",
     "service_name": "dailies-render",
     "shot": "SH200"
    },
    "values": [
     [
      1788108742,
      "1"
     ]
    ]
   }
  ]
 }
}"""
)

SH200_LOGS: dict[str, Any] = json.loads(
    r"""{
 "status": "success",
 "data": {
  "resultType": "streams",
  "result": [],
  "stats": {
   "summary": {
    "bytesProcessedPerSecond": 14585,
    "linesProcessedPerSecond": 34,
    "totalBytesProcessed": 1281,
    "totalLinesProcessed": 3,
    "execTime": 0.087827,
    "queueTime": 0.093527,
    "subqueries": 0,
    "totalEntriesReturned": 0,
    "splits": 13,
    "shards": 11,
    "totalPostFilterLines": 0,
    "totalStructuredMetadataBytesProcessed": 1050,
    "estimatedQueryBytes": 2684
   },
   "querier": {
    "store": {
     "totalChunksRef": 3,
     "totalChunksDownloaded": 3,
     "chunksDownloadTime": 91406466,
     "queryReferencedStructuredMetadata": true,
     "queryUsedV2Engine": false,
     "chunk": {
      "headChunkBytes": 0,
      "headChunkLines": 0,
      "decompressedBytes": 1281,
      "decompressedLines": 3,
      "compressedBytes": 363,
      "totalDuplicates": 0,
      "postFilterLines": 0,
      "headChunkStructuredMetadataBytes": 0,
      "decompressedStructuredMetadataBytes": 1050
     },
     "chunkRefsFetchTime": 21077458,
     "congestionControlLatency": 0,
     "pipelineWrapperFilteredLines": 0,
     "dataobj": {
      "prePredicateDecompressedRows": 0,
      "prePredicateDecompressedBytes": 0,
      "prePredicateDecompressedStructuredMetadataBytes": 0,
      "postPredicateRows": 0,
      "postPredicateDecompressedBytes": 0,
      "postPredicateStructuredMetadataBytes": 0,
      "postFilterRows": 0,
      "pagesScanned": 0,
      "pagesDownloaded": 0,
      "pagesDownloadedBytes": 0,
      "pageBatches": 0,
      "totalRowsAvailable": 0,
      "totalPageDownloadTime": 0,
      "wireBytesTransferred": 0
     }
    },
    "querierExecTime": 0.348125
   },
   "ingester": {
    "totalReached": 150,
    "totalChunksMatched": 0,
    "totalBatches": 150,
    "totalLinesSent": 0,
    "store": {
     "totalChunksRef": 0,
     "totalChunksDownloaded": 0,
     "chunksDownloadTime": 0,
     "queryReferencedStructuredMetadata": false,
     "queryUsedV2Engine": false,
     "chunk": {
      "headChunkBytes": 0,
      "headChunkLines": 0,
      "decompressedBytes": 0,
      "decompressedLines": 0,
      "compressedBytes": 0,
      "totalDuplicates": 0,
      "postFilterLines": 0,
      "headChunkStructuredMetadataBytes": 0,
      "decompressedStructuredMetadataBytes": 0
     },
     "chunkRefsFetchTime": 0,
     "congestionControlLatency": 0,
     "pipelineWrapperFilteredLines": 0,
     "dataobj": {
      "prePredicateDecompressedRows": 0,
      "prePredicateDecompressedBytes": 0,
      "prePredicateDecompressedStructuredMetadataBytes": 0,
      "postPredicateRows": 0,
      "postPredicateDecompressedBytes": 0,
      "postPredicateStructuredMetadataBytes": 0,
      "postFilterRows": 0,
      "pagesScanned": 0,
      "pagesDownloaded": 0,
      "pagesDownloadedBytes": 0,
      "pageBatches": 0,
      "totalRowsAvailable": 0,
      "totalPageDownloadTime": 0,
      "wireBytesTransferred": 0
     }
    },
    "recvWaitTime": 0.000821
   },
   "cache": {
    "chunk": {
     "entriesFound": 0,
     "entriesRequested": 3,
     "entriesStored": 6,
     "bytesReceived": 0,
     "bytesSent": 2099,
     "requests": 4,
     "downloadTime": 31023,
     "queryLengthServed": 0
    },
    "index": {
     "entriesFound": 0,
     "entriesRequested": 0,
     "entriesStored": 0,
     "bytesReceived": 0,
     "bytesSent": 0,
     "requests": 0,
     "downloadTime": 0,
     "queryLengthServed": 0
    },
    "result": {
     "entriesFound": 0,
     "entriesRequested": 12,
     "entriesStored": 12,
     "bytesReceived": 0,
     "bytesSent": 0,
     "requests": 24,
     "downloadTime": 10793219,
     "queryLengthServed": 0
    },
    "statsResult": {
     "entriesFound": 12,
     "entriesRequested": 12,
     "entriesStored": 5,
     "bytesReceived": 4320,
     "bytesSent": 0,
     "requests": 17,
     "downloadTime": 9906919,
     "queryLengthServed": 40687000000000
    },
    "volumeResult": {
     "entriesFound": 0,
     "entriesRequested": 0,
     "entriesStored": 0,
     "bytesReceived": 0,
     "bytesSent": 0,
     "requests": 0,
     "downloadTime": 0,
     "queryLengthServed": 0
    },
    "seriesResult": {
     "entriesFound": 0,
     "entriesRequested": 0,
     "entriesStored": 0,
     "bytesReceived": 0,
     "bytesSent": 0,
     "requests": 0,
     "downloadTime": 0,
     "queryLengthServed": 0
    },
    "labelResult": {
     "entriesFound": 0,
     "entriesRequested": 0,
     "entriesStored": 0,
     "bytesReceived": 0,
     "bytesSent": 0,
     "requests": 0,
     "downloadTime": 0,
     "queryLengthServed": 0
    },
    "instantMetricResult": {
     "entriesFound": 0,
     "entriesRequested": 0,
     "entriesStored": 0,
     "bytesReceived": 0,
     "bytesSent": 0,
     "requests": 0,
     "downloadTime": 0,
     "queryLengthServed": 0
    },
    "logResult": {
     "entriesFound": 0,
     "entriesRequested": 0,
     "entriesStored": 0,
     "bytesReceived": 0,
     "bytesSent": 0,
     "requests": 0,
     "downloadTime": 0,
     "queryLengthServed": 0
    },
    "taskResult": {
     "entriesFound": 0,
     "entriesRequested": 0,
     "entriesStored": 0,
     "bytesReceived": 0,
     "bytesSent": 0,
     "requests": 0,
     "downloadTime": 0,
     "queryLengthServed": 0
    }
   },
   "index": {
    "totalChunks": 3,
    "postFilterChunks": 3,
    "shardsDuration": 0,
    "usedBloomFilters": false,
    "totalStreams": 3,
    "chunkRefsLookupTime": 0.003142,
    "bloomFilterTime": 2.5e-05
   }
  }
 }
}"""
)
