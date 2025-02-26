#!/usr/bin/env python3
#
# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit test for "retry" (should be "fault injection") operations in the testbench."""

import json
import os
import re
import unittest

import testbench
from testbench import rest_server


UPLOAD_QUANTUM = 256 * 1024


class TestTestbenchRetry(unittest.TestCase):
    def setUp(self):
        rest_server.db.clear()
        self.client = rest_server.server.test_client()
        # Avoid magic buckets in the test
        os.environ.pop("GOOGLE_CLOUD_CPP_STORAGE_TEST_BUCKET_NAME", None)

    def test_retry_test_supported_operations(self):
        BUCKET_OPERATIONS = {
            "storage.buckets." + op
            for op in [
                "list",
                "insert",
                "get",
                "update",
                "patch",
                "delete",
                "getIamPolicy",
                "setIamPolicy",
                "testIamPermissions",
                "lockRetentionPolicy",
            ]
        }
        BUCKET_ACL_OPERATIONS = {
            "storage.bucket_acl." + op
            for op in ["list", "insert", "get", "update", "patch", "delete"]
        }
        BUCKET_DEFAULT_OBJECT_ACL_OPERATIONS = {
            "storage.default_object_acl." + op
            for op in ["list", "insert", "get", "update", "patch", "delete"]
        }
        NOTIFICATION_OPERATIONS = {
            "storage.notifications." + op for op in ["list", "insert", "get", "delete"]
        }
        OBJECT_OPERATIONS = {
            "storage.objects." + op
            for op in [
                "list",
                "insert",
                "get",
                "update",
                "patch",
                "delete",
                "compose",
                "copy",
                "rewrite",
            ]
        }
        OBJECT_ACL_OPERATIONS = {
            "storage.object_acl." + op
            for op in ["list", "insert", "get", "update", "patch", "delete"]
        }
        PROJECT_OPERATIONS = {"storage.serviceaccount.get"} | {
            "storage.hmacKey." + op
            for op in [
                "create",
                "list",
                "delete",
                "get",
                "update",
            ]
        }
        groups = {
            "buckets": BUCKET_OPERATIONS,
            "bucket_acl": BUCKET_ACL_OPERATIONS,
            "bucket_default_object_acl": BUCKET_DEFAULT_OBJECT_ACL_OPERATIONS,
            "notifications": NOTIFICATION_OPERATIONS,
            "objects": OBJECT_OPERATIONS,
            "object_acl": OBJECT_ACL_OPERATIONS,
            "project": PROJECT_OPERATIONS,
        }
        all = set(rest_server.db.supported_methods())
        for name, operations in groups.items():
            self.assertEqual(all, all | operations, msg=name)

    @staticmethod
    def _create_valid_chunk():
        line = "How vexingly quick daft zebras jump!"
        pad = (255 - len(line)) * " "
        line = line + pad + "\n"
        return 1024 * line

    def test_retry_test_crud(self):
        self.assertIn("storage.buckets.list", rest_server.db.supported_methods())
        response = self.client.post(
            "/retry_test",
            data=json.dumps({"instructions": {"storage.buckets.list": ["return-429"]}}),
        )
        self.assertEqual(response.status_code, 200, msg=response.data)
        self.assertTrue(
            response.headers.get("content-type").startswith("application/json")
        )
        create_rest = json.loads(response.data)
        self.assertIn("id", create_rest)

        response = self.client.get("/retry_test/" + create_rest.get("id"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers.get("content-type").startswith("application/json")
        )
        get_rest = json.loads(response.data)
        self.assertEqual(get_rest, create_rest)

        response = self.client.get("/retry_tests")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers.get("content-type").startswith("application/json")
        )
        list_rest = json.loads(response.data)
        ids = [test.get("id") for test in list_rest.get("retry_test", [])]
        self.assertEqual(ids, [create_rest.get("id")], msg=response.data)

        response = self.client.delete("/retry_test/" + create_rest.get("id"))
        self.assertEqual(response.status_code, 200)
        # Once deleted, getting the test should fail.
        response = self.client.get("/retry_test/" + create_rest.get("id"))
        self.assertEqual(response.status_code, 404)

    def test_retry_test_create_invalid(self):
        response = self.client.post("/retry_test", data=json.dumps({}))
        self.assertEqual(response.status_code, 400)

    def test_retry_test_get_notfound(self):
        response = self.client.get("/retry_test/invalid-id")
        self.assertEqual(response.status_code, 404)

    def test_retry_test_return_error(self):
        response = self.client.post(
            "/retry_test",
            data=json.dumps({"instructions": {"storage.buckets.list": ["return-429"]}}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers.get("content-type").startswith("application/json")
        )
        create_rest = json.loads(response.data)
        self.assertIn("id", create_rest)

        list_response = self.client.get(
            "/storage/v1/b",
            query_string={"project": "test-project-unused"},
            headers={"x-retry-test-id": create_rest.get("id")},
        )
        self.assertEqual(list_response.status_code, 429, msg=list_response.data)

    @staticmethod
    def _create_block(desired_kib):
        line = "A" * 127 + "\n"
        return int(desired_kib / len(line)) * line

    def test_retry_test_return_reset_connection(self):
        response = self.client.post(
            "/storage/v1/b", data=json.dumps({"name": "bucket-name"})
        )
        self.assertEqual(response.status_code, 200)
        # Use the XML API to inject an object with some data.
        media = self._create_block(256)
        response = self.client.put(
            "/bucket-name/256k.txt",
            content_type="text/plain",
            data=media,
        )
        self.assertEqual(response.status_code, 200)

        # Setup a failure for reading back the object.
        response = self.client.post(
            "/retry_test",
            data=json.dumps(
                {"instructions": {"storage.objects.get": ["return-reset-connection"]}}
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers.get("content-type").startswith("application/json")
        )
        create_rest = json.loads(response.data)
        self.assertIn("id", create_rest)
        id = create_rest.get("id")

        response = self.client.get(
            "/storage/v1/b/bucket-name/o/256k.txt",
            query_string={"alt": "media"},
            headers={"x-retry-test-id": id},
        )
        self.assertEqual(response.status_code, 500)
        error = json.loads(response.data)
        self.assertIn("connection reset by peer", error.get("message"))

    def test_retry_test_return_no_metadata_on_resumable_complete(self):
        response = self.client.post(
            "/storage/v1/b", data=json.dumps({"name": "bucket-name"})
        )
        self.assertEqual(response.status_code, 200)

        # Setup a error for resumable upload to respond with a 200 without object metadata returned
        bytes_returned = 0
        response = self.client.post(
            "/retry_test",
            data=json.dumps(
                {
                    "instructions": {
                        "storage.objects.insert": [
                            "return-broken-stream-final-chunk-after-%dB"
                            % bytes_returned
                        ]
                    }
                }
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers.get("content-type").startswith("application/json")
        )
        create_rest = json.loads(response.data)
        self.assertIn("id", create_rest)
        id = create_rest.get("id")

        response = self.client.post(
            "/upload/storage/v1/b/bucket-name/o",
            query_string={"uploadType": "resumable", "name": "256kobject"},
            headers={"x-retry-test-id": id},
        )
        self.assertEqual(response.status_code, 200)
        location = response.headers.get("location")

        response = self.client.put(
            location,
            data="test",
            headers={"x-retry-test-id": id},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers.get("content-type").startswith("application/json")
        )
        self.assertEqual(len(response.data), bytes_returned)

    def test_retry_test_return_no_metadata_on_resumable_multi_chunk_complete(self):
        response = self.client.post(
            "/storage/v1/b", data=json.dumps({"name": "bucket-name"})
        )
        self.assertEqual(response.status_code, 200)

        # Setup a error for resumable upload to respond with a 200 without object metadata returned
        bytes_returned = 10
        response = self.client.post(
            "/retry_test",
            data=json.dumps(
                {
                    "instructions": {
                        "storage.objects.insert": [
                            "return-broken-stream-final-chunk-after-%dB"
                            % bytes_returned
                        ]
                    }
                }
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers.get("content-type").startswith("application/json")
        )
        create_rest = json.loads(response.data)
        self.assertIn("id", create_rest)
        id = create_rest.get("id")
        chunk = self._create_valid_chunk()
        response = self.client.post(
            "/upload/storage/v1/b/bucket-name/o",
            query_string={"uploadType": "resumable", "name": "256kobject"},
            headers={
                "x-upload-content-length": "%d" % 2 * len(chunk),
                "x-retry-test-id": id,
            },
        )
        self.assertEqual(response.status_code, 200)
        location = response.headers.get("location")

        # Upload in chunks, but there is not need to specify the ending byte because
        #  it was set via the x-upload-content-length header.
        response = self.client.put(
            location,
            headers={
                "content-range": "bytes 0-{last:d}/*".format(last=len(chunk) - 1),
                "x-upload-content-length": "%d" % 2 * len(chunk),
                "x-retry-test-id": id,
            },
            data=chunk,
        )
        self.assertEqual(response.status_code, 308, msg=response.data)

        chunk = self._create_valid_chunk()
        response = self.client.put(
            location,
            headers={
                "content-range": "bytes {last:d}-*/*".format(last=len(chunk) - 1),
                "x-retry-test-id": id,
            },
            data=chunk,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers.get("content-type").startswith("application/json")
        )
        self.assertEqual(len(response.data), bytes_returned)

    def test_retry_test_return_broken_stream(self):
        response = self.client.post(
            "/storage/v1/b", data=json.dumps({"name": "bucket-name"})
        )
        self.assertEqual(response.status_code, 200)
        # Use the XML API to inject an object with some data.
        media = self._create_block(256)
        response = self.client.put(
            "/bucket-name/256k.txt",
            content_type="text/plain",
            data=media,
        )
        self.assertEqual(response.status_code, 200)

        # Setup a failure for reading back the object.
        response = self.client.post(
            "/retry_test",
            data=json.dumps(
                {"instructions": {"storage.objects.get": ["return-broken-stream"]}}
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers.get("content-type").startswith("application/json")
        )
        create_rest = json.loads(response.data)
        self.assertIn("id", create_rest)
        id = create_rest.get("id")

        response = self.client.get(
            "/storage/v1/b/bucket-name/o/256k.txt",
            query_string={"alt": "media"},
            headers={"x-retry-test-id": id},
        )
        with self.assertRaises(testbench.error.RestException) as ex:
            _ = len(response.data)
        self.assertIn("broken stream", ex.exception.msg)

    def test_retry_test_return_broken_stream_after_bytes(self):
        response = self.client.post(
            "/storage/v1/b", data=json.dumps({"name": "bucket-name"})
        )
        self.assertEqual(response.status_code, 200)
        # Use the XML API to inject a larger object and smaller object.
        media = self._create_block(UPLOAD_QUANTUM)
        blob_larger = self.client.put(
            "/bucket-name/256k.txt",
            content_type="text/plain",
            data=media,
        )
        self.assertEqual(blob_larger.status_code, 200)

        media = self._create_block(128)
        blob_smaller = self.client.put(
            "/bucket-name/128.txt",
            content_type="text/plain",
            data=media,
        )
        self.assertEqual(blob_smaller.status_code, 200)

        # Setup a failure for reading back the object.
        response = self.client.post(
            "/retry_test",
            data=json.dumps(
                {
                    "instructions": {
                        "storage.objects.get": ["return-broken-stream-after-256K"]
                    }
                }
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers.get("content-type").startswith("application/json")
        )
        create_rest = json.loads(response.data)
        self.assertIn("id", create_rest)
        id = create_rest.get("id")

        # The 128-bytes file is too small to trigger the "return-504-after-256K" fault injection.
        response = self.client.get(
            "/storage/v1/b/bucket-name/o/128.txt",
            query_string={"alt": "media"},
            headers={"x-retry-test-id": id},
        )
        self.assertEqual(response.status_code, 200, msg=response.data)

        # The 256KiB file triggers the "return-broken-stream-after-256K" fault injection.
        response = self.client.get(
            "/storage/v1/b/bucket-name/o/256k.txt",
            query_string={"alt": "media"},
            headers={"x-retry-test-id": id},
        )
        with self.assertRaises(testbench.error.RestException) as ex:
            _ = len(response.data)
        self.assertIn("broken stream", ex.exception.msg)

    def test_retry_test_return_error_after_bytes(self):
        response = self.client.post(
            "/storage/v1/b", data=json.dumps({"name": "bucket-name"})
        )
        self.assertEqual(response.status_code, 200)
        # Use the XML API to inject an object with some data.
        media = self._create_block(256)
        response = self.client.put(
            "/bucket-name/256k.txt",
            content_type="text/plain",
            data=media,
        )
        self.assertEqual(response.status_code, 200)

        # Setup a failure for reading back the object.
        response = self.client.post(
            "/retry_test",
            data=json.dumps(
                {"instructions": {"storage.objects.insert": ["return-504-after-256K"]}}
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers.get("content-type").startswith("application/json")
        )
        create_rest = json.loads(response.data)
        self.assertIn("id", create_rest)
        id = create_rest.get("id")

        response = self.client.post(
            "/upload/storage/v1/b/bucket-name/o",
            query_string={"uploadType": "resumable", "name": "will-fail"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        location = response.headers.get("location")
        self.assertIn("upload_id=", location)
        match = re.search("[&?]upload_id=([^&]+)", location)
        self.assertIsNotNone(match, msg=location)
        upload_id = match.group(1)

        chunk = self._create_block(UPLOAD_QUANTUM)
        self.assertEqual(len(chunk), UPLOAD_QUANTUM)

        response = self.client.put(
            "/upload/storage/v1/b/bucket-name/o",
            query_string={"upload_id": upload_id},
            headers={
                "content-range": "bytes 0-{len:d}/*".format(len=UPLOAD_QUANTUM - 1),
                "x-retry-test-id": id,
            },
            data=chunk,
        )
        self.assertEqual(response.status_code, 308, msg=response.data)
        self.assertIn("range", response.headers)
        self.assertEqual(
            response.headers.get("range"), "bytes=0-%d" % (UPLOAD_QUANTUM - 1)
        )

        response = self.client.put(
            "/upload/storage/v1/b/bucket-name/o",
            query_string={"upload_id": upload_id},
            headers={
                "content-range": "bytes {beg:d}-{end:d}/*".format(
                    beg=UPLOAD_QUANTUM, end=2 * UPLOAD_QUANTUM - 1
                ),
                "x-retry-test-id": id,
            },
            data=chunk,
        )
        self.assertEqual(response.status_code, 504, msg=response.data)


if __name__ == "__main__":
    unittest.main()
