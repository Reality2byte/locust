from __future__ import annotations

import locust
from locust import LoadTestShape, constant, stats
from locust.argument_parser import get_parser, parse_options
from locust.env import Environment
from locust.log import LogReader
from locust.runners import Runner
from locust.stats import StatsCSVFileWriter
from locust.user import User, task
from locust.web import WebUI

import csv
import json
import logging
import os
import traceback
from io import StringIO
from tempfile import NamedTemporaryFile

import gevent
import requests
from flask_login import UserMixin
from pyquery import PyQuery as pq

from .testcases import LocustTestCase
from .util import create_tls_cert


class _HeaderCheckMixin:
    def _check_csv_headers(self, headers, exp_fn_prefix):
        # Check common headers for csv file download request
        self.assertIn("Content-Type", headers)
        content_type = headers["Content-Type"]
        self.assertIn("text/csv", content_type)

        self.assertIn("Content-disposition", headers)
        disposition = headers[
            "Content-disposition"
        ]  # e.g.: 'attachment; filename=requests_full_history_1597586811.5084946.csv'
        self.assertIn(exp_fn_prefix, disposition)


class TestWebUI(LocustTestCase, _HeaderCheckMixin):
    def setUp(self):
        super().setUp()

        parser = get_parser(default_config_files=[])
        self.environment.parsed_options = parser.parse_args([])
        self.stats = self.environment.stats

        self.web_ui = self.environment.create_web_ui("127.0.0.1", 0)
        self.web_ui.app.view_functions["locust.request_stats"].clear_cache()
        gevent.sleep(0.01)
        self.web_port = self.web_ui.server.server_port

    def tearDown(self):
        super().tearDown()
        self.web_ui.stop()
        self.runner.quit()

    def test_web_ui_reference_on_environment(self):
        self.assertEqual(self.web_ui, self.environment.web_ui)

    def test_web_ui_no_runner(self):
        env = Environment()
        web_ui = WebUI(env, "127.0.0.1", 0)
        gevent.sleep(0.01)
        try:
            response = requests.get("http://127.0.0.1:%i/" % web_ui.server.server_port)
            self.assertEqual(500, response.status_code)
            self.assertEqual("Error: Locust Environment does not have any runner", response.text)
        finally:
            web_ui.stop()

    def test_index(self):
        self.assertEqual(self.web_ui, self.environment.web_ui)

        html_to_option = {
            "num_users": ["-u", "100"],
            "spawn_rate": ["-r", "10.0"],
        }

        response = requests.get("http://127.0.0.1:%i/" % self.web_port)
        d = pq(response.content.decode("utf-8"))

        self.assertEqual(200, response.status_code)
        self.assertTrue(d("#root"))

        for html_name_to_test in html_to_option.keys():
            # Test that setting each spawn option individually populates the corresponding field in the html, and none of the others
            self.environment.parsed_options = parse_options(html_to_option[html_name_to_test])

            response = requests.get("http://127.0.0.1:%i/" % self.web_port)
            self.assertEqual(200, response.status_code)

            d = pq(response.content.decode("utf-8"))

            self.assertIn(f'"{html_name_to_test}": {html_to_option[html_name_to_test][1]}', str(d("script")))

    def test_index_with_spawn_options(self):
        html_to_option = {
            "num_users": ["-u", "100"],
            "spawn_rate": ["-r", "10.0"],
        }

        for html_name_to_test in html_to_option.keys():
            self.environment.parsed_options = parse_options(html_to_option[html_name_to_test])

            response = requests.get("http://127.0.0.1:%i/" % self.web_port)
            self.assertEqual(200, response.status_code)

            d = pq(response.content.decode("utf-8"))

            self.assertIn(f'"{html_name_to_test}": {html_to_option[html_name_to_test][1]}', str(d))

    def test_stats_no_data(self):
        self.assertEqual(200, requests.get("http://127.0.0.1:%i/stats/requests" % self.web_port).status_code)

    def test_stats(self):
        self.stats.log_request("GET", "/<html>", 120, 5612)
        response = requests.get("http://127.0.0.1:%i/stats/requests" % self.web_port)
        self.assertEqual(200, response.status_code)

        data = json.loads(response.text)
        self.assertEqual(2, len(data["stats"]))  # one entry plus Aggregated
        self.assertEqual("/<html>", data["stats"][0]["name"])
        self.assertEqual("GET", data["stats"][0]["method"])

        self.assertEqual("Aggregated", data["stats"][1]["name"])
        self.assertEqual(1, data["stats"][1]["num_requests"])

    def test_stats_cache(self):
        self.stats.log_request("GET", "/test", 120, 5612)
        response = requests.get("http://127.0.0.1:%i/stats/requests" % self.web_port)
        self.assertEqual(200, response.status_code)
        data = json.loads(response.text)
        self.assertEqual(2, len(data["stats"]))  # one entry plus Aggregated

        # add another entry
        self.stats.log_request("GET", "/test2", 120, 5612)
        data = json.loads(requests.get("http://127.0.0.1:%i/stats/requests" % self.web_port).text)
        self.assertEqual(2, len(data["stats"]))  # old value should be cached now

        self.web_ui.app.view_functions["locust.request_stats"].clear_cache()

        data = json.loads(requests.get("http://127.0.0.1:%i/stats/requests" % self.web_port).text)
        self.assertEqual(3, len(data["stats"]))  # this should no longer be cached

    def test_stats_rounding(self):
        self.stats.log_request("GET", "/test", 1.39764125, 2)
        self.stats.log_request("GET", "/test", 999.9764125, 1000)
        response = requests.get("http://127.0.0.1:%i/stats/requests" % self.web_port)
        self.assertEqual(200, response.status_code)

        data = json.loads(response.text)
        self.assertEqual(1, data["stats"][0]["min_response_time"])
        self.assertEqual(1000, data["stats"][0]["max_response_time"])

    def test_request_stats_csv(self):
        self.stats.log_request("GET", "/test2", 120, 5612)
        response = requests.get("http://127.0.0.1:%i/stats/requests/csv" % self.web_port)
        self.assertEqual(200, response.status_code)
        self._check_csv_headers(response.headers, "requests")

    def test_request_stats_full_history_csv_not_present(self):
        self.stats.log_request("GET", "/test2", 120, 5612)
        response = requests.get("http://127.0.0.1:%i/stats/requests_full_history/csv" % self.web_port)
        self.assertEqual(404, response.status_code)

    def test_failure_stats_csv(self):
        self.stats.log_error("GET", "/", Exception("Error1337"))
        response = requests.get("http://127.0.0.1:%i/stats/failures/csv" % self.web_port)
        self.assertEqual(200, response.status_code)
        self._check_csv_headers(response.headers, "failures")

    def test_request_stats_with_errors(self):
        self.stats.log_error("GET", "/", Exception("Error with special characters {'foo':'bar'}"))
        response = requests.get("http://127.0.0.1:%i/stats/requests" % self.web_port)
        self.assertEqual(200, response.status_code)

        # escaped, old school
        # self.assertIn(
        #     '"Exception(&quot;Error with special characters{&#x27;foo&#x27;:&#x27;bar&#x27;}&quot;)"', response.text
        # )

        # not html escaping, leave that to the frontend
        self.assertIn("\"Exception(\\\"Error with special characters {'foo':'bar'}\\\")", response.text)

    def test_reset_stats(self):
        try:
            raise Exception("A cool test exception")
        except Exception as e:
            tb = e.__traceback__
            self.runner.log_exception("local", str(e), "".join(traceback.format_tb(tb)))
            self.runner.log_exception("local", str(e), "".join(traceback.format_tb(tb)))

        self.stats.log_request("GET", "/test", 120, 5612)
        self.stats.log_error("GET", "/", Exception("Error1337"))

        response = requests.get("http://127.0.0.1:%i/stats/reset" % self.web_port)

        self.assertEqual(200, response.status_code)

        self.assertEqual({}, self.stats.errors)
        self.assertEqual({}, self.runner.exceptions)

        self.assertEqual(0, self.stats.get("/", "GET").num_requests)
        self.assertEqual(0, self.stats.get("/", "GET").num_failures)
        self.assertEqual(0, self.stats.get("/test", "GET").num_requests)
        self.assertEqual(0, self.stats.get("/test", "GET").num_failures)

    def test_exceptions(self):
        try:
            raise Exception("A cool test exception")
        except Exception as e:
            tb = e.__traceback__
            self.runner.log_exception("local", str(e), "".join(traceback.format_tb(tb)))
            self.runner.log_exception("local", str(e), "".join(traceback.format_tb(tb)))

        response = requests.get("http://127.0.0.1:%i/exceptions" % self.web_port)
        self.assertEqual(200, response.status_code)
        self.assertIn("A cool test exception", response.text)

        response = requests.get("http://127.0.0.1:%i/stats/requests" % self.web_port)
        self.assertEqual(200, response.status_code)

    def test_exceptions_csv(self):
        try:
            raise Exception("Test exception")
        except Exception as e:
            tb = e.__traceback__
            self.runner.log_exception("local", str(e), "".join(traceback.format_tb(tb)))
            self.runner.log_exception("local", str(e), "".join(traceback.format_tb(tb)))

        response = requests.get("http://127.0.0.1:%i/exceptions/csv" % self.web_port)
        self.assertEqual(200, response.status_code)
        self._check_csv_headers(response.headers, "exceptions")

        reader = csv.reader(StringIO(response.text))
        rows = []
        for row in reader:
            rows.append(row)

        self.assertEqual(2, len(rows))
        self.assertEqual("Test exception", rows[1][1])
        self.assertEqual(2, int(rows[1][0]), "Exception count should be 2")

    def test_swarm_host_value_specified(self):
        class MyUser(User):
            wait_time = constant(1)

            @task(1)
            def my_task(self):
                pass

        self.environment.user_classes = [MyUser]
        self.environment.web_ui.parsed_options = parse_options()
        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={"user_count": 5, "spawn_rate": 5, "host": "https://localhost"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("https://localhost", response.json()["host"])
        self.assertEqual(self.environment.host, "https://localhost")
        # stop
        gevent.sleep(1)
        response = requests.get("http://127.0.0.1:%i/stop" % self.web_port)
        self.assertEqual(response.json()["message"], "Test stopped")
        # and swarm again, with new host
        gevent.sleep(1)
        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={"user_count": 5, "spawn_rate": 5, "host": "https://localhost/other"},
        )
        gevent.sleep(1)
        self.assertEqual(200, response.status_code)
        self.assertEqual("https://localhost/other", response.json()["host"])
        self.assertEqual(self.environment.host, "https://localhost/other")

    def test_swarm_userclass_specified(self):
        class User1(User):
            wait_time = constant(1)

            @task
            def t(self):
                pass

        class User2(User):
            wait_time = constant(1)

            @task
            def t(self):
                pass

        self.environment.web_ui.userclass_picker_is_active = True
        self.environment.available_user_classes = {"User1": User1, "User2": User2}

        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={
                "user_count": 5,
                "spawn_rate": 5,
                "host": "https://localhost",
                "user_classes": "User1",
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("https://localhost", response.json()["host"])
        self.assertEqual(self.environment.host, "https://localhost")
        self.assertEqual(["User1"], response.json()["user_classes"])

        # stop
        gevent.sleep(1)
        response = requests.get("http://127.0.0.1:%i/stop" % self.web_port)
        self.assertEqual(response.json()["message"], "Test stopped")

        # and swarm again, with new locustfile
        gevent.sleep(1)
        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={
                "user_count": 5,
                "spawn_rate": 5,
                "host": "https://localhost",
                "user_classes": "User2",
            },
        )
        gevent.sleep(1)
        self.assertEqual(200, response.status_code)
        self.assertEqual("https://localhost", response.json()["host"])
        self.assertEqual(self.environment.host, "https://localhost")
        self.assertEqual(["User2"], response.json()["user_classes"])

    def test_swarm_multiple_userclasses_specified(self):
        class User1(User):
            wait_time = constant(1)

            @task
            def t(self):
                pass

        class User2(User):
            wait_time = constant(1)

            @task
            def t(self):
                pass

        self.environment.web_ui.userclass_picker_is_active = True
        self.environment.available_user_classes = {"User1": User1, "User2": User2}

        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={
                "user_count": 5,
                "spawn_rate": 5,
                "host": "https://localhost",
                "user_classes": ["User1", "User2"],
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("https://localhost", response.json()["host"])
        self.assertEqual(self.environment.host, "https://localhost")
        self.assertListEqual(["User1", "User2"], response.json()["user_classes"])

        self.assertIsNotNone(self.environment.locustfile, "verify locustfile is not empty")
        self.assertEqual(self.environment.locustfile, "User1,User2", "Verify locustfile variable used in web ui title")

        # stop
        gevent.sleep(1)
        response = requests.get("http://127.0.0.1:%i/stop" % self.web_port)
        self.assertEqual(response.json()["message"], "Test stopped")

    def test_swarm_updates_parsed_options_when_single_userclass_specified(self):
        """
        This test validates that environment.parsed_options.user_classes isn't overwritten
        when /swarm is hit with 'user_classes' in the data.
        """

        class User1(User):
            wait_time = constant(1)

            @task
            def t(self):
                pass

        class User2(User):
            wait_time = constant(1)

            @task
            def t(self):
                pass

        self.environment.web_ui.userclass_picker_is_active = True
        self.environment.available_user_classes = {"User1": User1, "User2": User2}

        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={
                "user_count": 5,
                "spawn_rate": 5,
                "host": "https://localhost",
                "user_classes": ["User1"],
            },
        )
        self.assertListEqual(["User1"], response.json()["user_classes"])

        # stop
        gevent.sleep(1)
        response = requests.get("http://127.0.0.1:%i/stop" % self.web_port)
        self.assertEqual(response.json()["message"], "Test stopped")

        # Checking environment.parsed_options.user_classes was updated
        self.assertListEqual(self.environment.parsed_options.user_classes, ["User1"])

    def test_swarm_updates_parsed_options_when_multiple_userclasses_specified(self):
        """
        This test validates that environment.parsed_options.user_classes isn't overwritten
        when /swarm is hit with 'user_classes' in the data.
        """

        class User1(User):
            wait_time = constant(1)

            @task
            def t(self):
                pass

        class User2(User):
            wait_time = constant(1)

            @task
            def t(self):
                pass

        self.environment.web_ui.userclass_picker_is_active = True
        self.environment.available_user_classes = {"User1": User1, "User2": User2}

        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={
                "user_count": 5,
                "spawn_rate": 5,
                "host": "https://localhost",
                "user_classes": ["User1", "User2"],
            },
        )
        self.assertListEqual(["User1", "User2"], response.json()["user_classes"])

        # stop
        gevent.sleep(1)
        response = requests.get("http://127.0.0.1:%i/stop" % self.web_port)
        self.assertEqual(response.json()["message"], "Test stopped")

        # Checking environment.parsed_options.user_classes was updated
        self.assertListEqual(self.environment.parsed_options.user_classes, ["User1", "User2"])

    def test_swarm_defaults_to_all_available_userclasses_when_userclass_picker_is_active_and_no_userclass_in_payload(
        self,
    ):
        class User1(User):
            wait_time = constant(1)

            @task
            def t(self):
                pass

        class User2(User):
            wait_time = constant(1)

            @task
            def t(self):
                pass

        self.environment.web_ui.userclass_picker_is_active = True
        self.environment.available_user_classes = {"User1": User1, "User2": User2}
        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={
                "user_count": 5,
                "spawn_rate": 5,
                "host": "https://localhost",
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("https://localhost", response.json()["host"])
        self.assertEqual(self.environment.host, "https://localhost")
        self.assertListEqual(["User1", "User2"], response.json()["user_classes"])

        # stop
        gevent.sleep(1)
        response = requests.get("http://127.0.0.1:%i/stop" % self.web_port)
        self.assertEqual(response.json()["message"], "Test stopped")

    def test_swarm_uses_pre_selected_user_classes_when_empty_payload_and_test_is_already_running_with_class_picker(
        self,
    ):
        # This test validates that the correct User Classes are used when editing a running test
        class User1(User):
            wait_time = constant(1)

            @task
            def t(self):
                pass

        class User2(User):
            wait_time = constant(1)

            @task
            def t(self):
                pass

        self.environment.web_ui.userclass_picker_is_active = True
        self.environment.available_user_classes = {"User1": User1, "User2": User2}
        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={
                "user_count": 5,
                "spawn_rate": 5,
                "host": "https://localhost",
                "user_classes": ["User1"],
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("https://localhost", response.json()["host"])
        self.assertEqual(self.environment.host, "https://localhost")
        self.assertListEqual(["User1"], response.json()["user_classes"])

        # simulating edit running load test
        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={
                "user_count": 10,
                "spawn_rate": 10,
            },
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("https://localhost", response.json()["host"])
        self.assertEqual(self.environment.host, "https://localhost")
        self.assertListEqual(["User1"], response.json()["user_classes"])

        # stop
        gevent.sleep(1)
        response = requests.get("http://127.0.0.1:%i/stop" % self.web_port)
        self.assertEqual(response.json()["message"], "Test stopped")

    def test_swarm_error_when_userclass_picker_is_active_but_no_available_userclasses(self):
        self.environment.web_ui.userclass_picker_is_active = True
        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={
                "user_count": 5,
                "spawn_rate": 5,
                "host": "https://localhost",
                "user_classes": "User1",
            },
        )

        expected_error_message = "UserClass picker is active but there are no available UserClasses"
        self.assertEqual(False, response.json()["success"])
        self.assertEqual(expected_error_message, response.json()["message"])

    def test_swarm_shape_class_specified(self):
        class User1(User):
            wait_time = constant(1)

            @task
            def t(self):
                pass

        class User2(User):
            wait_time = constant(1)

            @task
            def t(self):
                pass

        class TestShape1(LoadTestShape):
            def tick(self):
                run_time = self.get_run_time()
                if run_time < 10:
                    return 4, 4
                else:
                    return None

        class TestShape2(LoadTestShape):
            def tick(self):
                run_time = self.get_run_time()
                if run_time < 10:
                    return 4, 4
                else:
                    return None

        self.environment.web_ui.userclass_picker_is_active = True
        self.environment.available_user_classes = {"User1": User1, "User2": User2}
        self.environment.available_shape_classes = {"TestShape1": TestShape1(), "TestShape2": TestShape2()}
        self.environment.shape_class = TestShape1()

        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={
                "user_count": 5,
                "spawn_rate": 5,
                "host": "https://localhost",
                "user_classes": "User1",
                "shape_class": "TestShape2",
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("https://localhost", response.json()["host"])
        self.assertEqual(self.environment.host, "https://localhost")
        assert isinstance(self.environment.shape_class, TestShape2)

        # stop
        gevent.sleep(1)
        response = requests.get("http://127.0.0.1:%i/stop" % self.web_port)
        self.assertEqual(response.json()["message"], "Test stopped")

    def test_swarm_shape_class_defaults_to_none_when_userclass_picker_is_active(self):
        class User1(User):
            wait_time = constant(1)

            @task
            def t(self):
                pass

        class User2(User):
            wait_time = constant(1)

            @task
            def t(self):
                pass

        class TestShape(LoadTestShape):
            def tick(self):
                run_time = self.get_run_time()
                if run_time < 10:
                    return 4, 4
                else:
                    return None

        test_shape_instance = TestShape()

        self.environment.web_ui.userclass_picker_is_active = True
        self.environment.available_user_classes = {"User1": User1, "User2": User2}
        self.environment.available_shape_classes = {"TestShape": test_shape_instance}
        self.environment.shape_class = test_shape_instance

        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={
                "user_count": 5,
                "spawn_rate": 5,
                "host": "https://localhost",
                "user_classes": "User1",
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("https://localhost", response.json()["host"])
        self.assertEqual(self.environment.host, "https://localhost")
        self.assertIsNone(self.environment.shape_class)

        # stop
        gevent.sleep(1)
        response = requests.get("http://127.0.0.1:%i/stop" % self.web_port)
        self.assertEqual(response.json()["message"], "Test stopped")

    def test_swarm_shape_class_is_updated_when_userclass_picker_is_active(self):
        class User1(User):
            pass

        class TestShape(LoadTestShape):
            def tick(self):
                pass

        test_shape_instance = TestShape()

        self.environment.web_ui.userclass_picker_is_active = True
        self.environment.available_user_classes = {"User1": User1}
        self.environment.available_shape_classes = {"TestShape": test_shape_instance}
        self.environment.shape_class = None

        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={
                "user_count": 5,
                "spawn_rate": 5,
                "host": "https://localhost",
                "user_classes": "User1",
                "shape_class": "TestShape",
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(test_shape_instance, self.environment.shape_class)
        self.assertIsNotNone(test_shape_instance.runner)

        # stop
        gevent.sleep(1)
        response = requests.get("http://127.0.0.1:%i/stop" % self.web_port)
        self.assertEqual(response.json()["message"], "Test stopped")

    def test_swarm_userclass_shapeclass_ignored_when_userclass_picker_is_inactive(self):
        class User1(User):
            wait_time = constant(1)

            @task
            def t(self):
                pass

        class User2(User):
            wait_time = constant(1)

            @task
            def t(self):
                pass

        class TestShape(LoadTestShape):
            def tick(self):
                run_time = self.get_run_time()
                if run_time < 10:
                    return 4, 4
                else:
                    return None

        self.environment.web_ui.userclass_picker_is_active = False
        self.environment.user_classes = [User1, User2]
        self.environment.available_user_classes = {"User1": User1, "User2": User2}
        self.environment.available_shape_classes = {"TestShape": TestShape()}
        self.environment.shape_class = None

        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={
                "user_count": 5,
                "spawn_rate": 5,
                "host": "https://localhost",
                "user_classes": "User1",
                "shape_class": "TestShape",
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("https://localhost", response.json()["host"])
        self.assertEqual(self.environment.host, "https://localhost")
        self.assertListEqual(self.environment.user_classes, [User1, User2])
        self.assertIsNone(self.environment.shape_class)

        # stop
        gevent.sleep(1)
        response = requests.get("http://127.0.0.1:%i/stop" % self.web_port)
        self.assertEqual(response.json()["message"], "Test stopped")

    def test_swarm_custom_argument_without_default_value(self):
        my_dict = {}

        class MyUser(User):
            host = "http://example.com"
            wait_time = constant(1)

            @task(1)
            def my_task(self):
                my_dict["val"] = self.environment.parsed_options.my_argument

        @locust.events.init_command_line_parser.add_listener
        def _(parser):
            parser.add_argument("--my-argument", type=int, help="Give me a number")

        parsed_options = parse_options()
        self.environment.user_classes = [MyUser]
        self.environment.parsed_options = parsed_options
        self.environment.web_ui.parsed_options = parsed_options
        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={"user_count": 1, "spawn_rate": 1, "host": "", "my_argument": "42"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("42", my_dict["val"])

    def test_swarm_custom_argument_with_default_value(self):
        my_dict = {}

        class MyUser(User):
            host = "http://example.com"
            wait_time = constant(1)

            @task(1)
            def my_task(self):
                my_dict["val"] = self.environment.parsed_options.my_argument

        @locust.events.init_command_line_parser.add_listener
        def _(parser):
            parser.add_argument("--my-argument", type=int, help="Give me a number", default=24)

        parsed_options = parse_options()
        self.environment.user_classes = [MyUser]
        self.environment.parsed_options = parsed_options
        self.environment.web_ui.parsed_options = parsed_options
        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={"user_count": 1, "spawn_rate": 1, "host": "", "my_argument": "42"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(42, my_dict["val"])

    def test_swarm_custom_argument_with_default_list_str_value(self):
        my_dict = {}

        class MyUser(User):
            host = "http://example.com"
            wait_time = constant(1)

            @task(1)
            def my_task(self):
                my_dict["val"] = self.environment.parsed_options.my_argument

        @locust.events.init_command_line_parser.add_listener
        def _(parser):
            parser.add_argument("--my-argument", default=["*"], help="Give me a number", action="append")

        parsed_options = parse_options()
        self.environment.user_classes = [MyUser]
        self.environment.parsed_options = parsed_options
        self.environment.web_ui.parsed_options = parsed_options
        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={"user_count": 1, "spawn_rate": 1, "host": "", "my_argument": "42,24"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(["42", "24"], my_dict["val"])

    def test_swarm_custom_argument_with_default_list_int_value(self):
        my_dict = {}

        class MyUser(User):
            host = "http://example.com"
            wait_time = constant(1)

            @task(1)
            def my_task(self):
                my_dict["val"] = self.environment.parsed_options.my_argument

        @locust.events.init_command_line_parser.add_listener
        def _(parser):
            parser.add_argument("--my-argument", default=[1], help="Give me a number", action="append")

        parsed_options = parse_options()
        self.environment.user_classes = [MyUser]
        self.environment.parsed_options = parsed_options
        self.environment.web_ui.parsed_options = parsed_options
        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={"user_count": 1, "spawn_rate": 1, "host": "", "my_argument": "42,24"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual([42, 24], my_dict["val"])

    def test_swarm_override_command_line_argument(self):
        my_dict = {}

        class MyUser(User):
            host = "http://example.com"
            wait_time = constant(1)

            @task(1)
            def my_task(self):
                my_dict["val"] = self.environment.parsed_options.my_argument

        @locust.events.init_command_line_parser.add_listener
        def _(parser):
            parser.add_argument("--my-argument", type=int, help="Give me a number")

        parsed_options = parse_options(args=["--my-argument", "24"])
        self.environment.user_classes = [MyUser]
        self.environment.parsed_options = parsed_options
        self.environment.web_ui.parsed_options = parsed_options
        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={"user_count": 1, "spawn_rate": 1, "host": "", "my_argument": "42"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(42, my_dict["val"])

    def test_swarm_host_value_not_specified(self):
        class MyUser(User):
            wait_time = constant(1)

            @task(1)
            def my_task(self):
                pass

        self.environment.user_classes = [MyUser]
        self.environment.web_ui.parsed_options = parse_options()
        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={"user_count": 5, "spawn_rate": 5},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(None, response.json()["host"])
        self.assertEqual(self.environment.host, None)

    def test_swarm_run_time(self):
        class MyUser(User):
            wait_time = constant(1)

            @task(1)
            def my_task(self):
                pass

        self.environment.user_classes = [MyUser]
        self.environment.web_ui.parsed_options = parse_options()
        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={"user_count": 5, "spawn_rate": 5, "host": "https://localhost", "run_time": "1s"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("https://localhost", response.json()["host"])
        self.assertEqual(self.environment.host, "https://localhost")
        self.assertEqual(1, response.json()["run_time"])
        # wait for test to run
        gevent.sleep(3)
        response = requests.get("http://127.0.0.1:%i/stats/requests" % self.web_port)
        self.assertEqual("stopped", response.json()["state"])

    def test_swarm_run_time_invalid_input(self):
        class MyUser(User):
            wait_time = constant(1)

            @task(1)
            def my_task(self):
                pass

        self.environment.user_classes = [MyUser]
        self.environment.web_ui.parsed_options = parse_options()
        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={"user_count": 5, "spawn_rate": 5, "host": "https://localhost", "run_time": "bad"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(False, response.json()["success"])
        self.assertEqual(self.environment.host, "https://localhost")
        self.assertEqual(
            "Valid run_time formats are : 20, 20s, 3m, 2h, 1h20m, 3h30m10s, etc.", response.json()["message"]
        )
        # verify test was not started
        response = requests.get("http://127.0.0.1:%i/stats/requests" % self.web_port)
        self.assertEqual("ready", response.json()["state"])
        requests.get("http://127.0.0.1:%i/stats/reset" % self.web_port)

    def test_swarm_run_time_empty_input(self):
        class MyUser(User):
            wait_time = constant(1)

            @task(1)
            def my_task(self):
                pass

        self.environment.user_classes = [MyUser]
        self.environment.web_ui.parsed_options = parse_options()
        response = requests.post(
            "http://127.0.0.1:%i/swarm" % self.web_port,
            data={"user_count": 5, "spawn_rate": 5, "host": "https://localhost", "run_time": ""},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("https://localhost", response.json()["host"])
        self.assertEqual(self.environment.host, "https://localhost")

        # verify test is running
        gevent.sleep(1)
        response = requests.get("http://127.0.0.1:%i/stats/requests" % self.web_port)
        self.assertEqual("running", response.json()["state"])

        # stop
        response = requests.get("http://127.0.0.1:%i/stop" % self.web_port)

    def test_host_value_from_user_class(self):
        class MyUser(User):
            host = "http://example.com"

        self.environment.user_classes = [MyUser]
        response = requests.get("http://127.0.0.1:%i/" % self.web_port)
        self.assertEqual(200, response.status_code)
        self.assertIn("http://example.com", response.content.decode("utf-8"))
        self.assertNotIn("setting this will override the host on all User classes", response.content.decode("utf-8"))

    def test_host_value_from_multiple_user_classes(self):
        class MyUser(User):
            host = "http://example.com"

        class MyUser2(User):
            host = "http://example.com"

        self.environment.user_classes = [MyUser, MyUser2]
        response = requests.get("http://127.0.0.1:%i/" % self.web_port)
        self.assertEqual(200, response.status_code)
        self.assertIn("http://example.com", response.content.decode("utf-8"))
        self.assertNotIn("setting this will override the host on all User classes", response.content.decode("utf-8"))

    def test_host_value_from_multiple_user_classes_different_hosts(self):
        class MyUser(User):
            host = None

        class MyUser2(User):
            host = "http://example.com"

        self.environment.user_classes = [MyUser, MyUser2]
        response = requests.get("http://127.0.0.1:%i/" % self.web_port)
        self.assertEqual(200, response.status_code)
        self.assertNotIn("http://example.com", response.content.decode("utf-8"))

    def test_report_page(self):
        self.stats.log_request("GET", "/test", 120, 5612)
        r = requests.get("http://127.0.0.1:%i/stats/report" % self.web_port)

        d = pq(r.content.decode("utf-8"))

        self.assertEqual(200, r.status_code)
        self.assertIn('"host": "None"', str(d))
        self.assertIn('"num_requests": 1', str(d))
        self.assertIn('"is_report": true', str(d))
        self.assertIn('"show_download_link": true', str(d))

    def test_report_page_empty_stats(self):
        r = requests.get("http://127.0.0.1:%i/stats/report" % self.web_port)
        self.assertEqual(200, r.status_code)

    def test_report_download(self):
        self.stats.log_request("GET", "/test", 120, 5612)
        r = requests.get("http://127.0.0.1:%i/stats/report?download=1" % self.web_port)

        d = pq(r.content.decode("utf-8"))

        self.assertEqual(200, r.status_code)
        self.assertIn("attachment", r.headers.get("Content-Disposition", ""))
        self.assertIn('"show_download_link": false', str(d))

    def test_report_host(self):
        self.environment.host = "http://test.com"
        self.stats.log_request("GET", "/test", 120, 5612)
        r = requests.get("http://127.0.0.1:%i/stats/report" % self.web_port)

        d = pq(r.content.decode("utf-8"))

        self.assertEqual(200, r.status_code)
        self.assertIn('"host": "http://test.com"', str(d))

    def test_report_host2(self):
        class MyUser(User):
            host = "http://test2.com"

            @task
            def my_task(self):
                pass

        self.environment.host = None
        self.environment.user_classes = [MyUser]
        self.stats.log_request("GET", "/test", 120, 5612)
        r = requests.get("http://127.0.0.1:%i/stats/report" % self.web_port)

        d = pq(r.content.decode("utf-8"))

        self.assertEqual(200, r.status_code)
        self.assertIn('"host": "http://test2.com"', str(d))

    def test_report_exceptions(self):
        try:
            raise Exception("Test exception")
        except Exception as e:
            tb = e.__traceback__
            self.runner.log_exception("local", str(e), "".join(traceback.format_tb(tb)))
            self.runner.log_exception("local", str(e), "".join(traceback.format_tb(tb)))
        self.stats.log_request("GET", "/test", 120, 5612)
        r = requests.get("http://127.0.0.1:%i/stats/report" % self.web_port)

        d = pq(r.content.decode("utf-8"))

        self.assertIn('exceptions_statistics": [{"count": 2', str(d))

        # Prior to 088a98bf8ff4035a0de3becc8cd4e887d618af53, the "nodes" field for each exception in
        # "self.runner.exceptions" was accidentally mutated in "get_html_report" to a string.
        # This assertion reproduces the issue and it is left there to make sure there's no
        # regression in the future.
        self.assertTrue(
            isinstance(next(iter(self.runner.exceptions.values()))["nodes"], set), "exception object has been mutated"
        )

    def test_html_stats_report(self):
        self.environment.locustfile = "locust.py"
        self.environment.host = "http://localhost"

        response = requests.get("http://127.0.0.1:%i/stats/report" % self.web_port)
        self.assertEqual(200, response.status_code)

        d = pq(response.content.decode("utf-8"))

        self.assertTrue(d("#root"))
        self.assertIn('"locustfile": "locust.py"', str(d))
        self.assertIn('"host": "http://localhost"', str(d))

    def test_logs(self):
        log_handler = LogReader()
        log_handler.name = "log_reader"
        log_handler.setLevel(logging.INFO)
        logger = logging.getLogger("root")
        logger.addHandler(log_handler)
        log_line = "some log info"
        logger.info(log_line)

        response = requests.get("http://127.0.0.1:%i/logs" % self.web_port)

        self.assertIn(log_line, response.json().get("master"))

    def test_worker_logs(self):
        log_handler = LogReader()
        log_handler.name = "log_reader"
        log_handler.setLevel(logging.INFO)
        logger = logging.getLogger("root")
        logger.addHandler(log_handler)
        log_line = "some log info"
        logger.info(log_line)

        worker_id = "123"
        worker_log_line = "worker log"
        self.environment.update_worker_logs({"worker_id": worker_id, "logs": [worker_log_line]})

        response = requests.get("http://127.0.0.1:%i/logs" % self.web_port)

        self.assertIn(log_line, response.json().get("master"))
        self.assertIn(worker_log_line, response.json().get("workers").get(worker_id))

    def test_template_args(self):
        class MyUser(User):
            @task
            def do_something(self):
                self.client.get("/")

            host = "http://example.com"

        class MyUser2(User):
            host = "http://example.com"

        self.environment.user_classes = [MyUser, MyUser2]
        self.environment.available_user_classes = {"User1": MyUser, "User2": MyUser2}
        self.environment.available_user_tasks = {"User1": MyUser.tasks, "User2": MyUser2.tasks}

        users = {"User1": MyUser.json(), "User2": MyUser2.json()}
        available_user_tasks = {"User1": ["do_something"], "User2": []}

        self.web_ui.update_template_args()

        self.assertEqual(self.web_ui.template_args.get("users"), users)
        self.assertEqual(
            self.web_ui.template_args.get("available_user_classes"), sorted(self.environment.available_user_classes)
        )
        self.assertEqual(self.web_ui.template_args.get("available_user_tasks"), available_user_tasks)

    def test_update_user_endpoint(self):
        class MyUser(User):
            @task
            def my_task(self):
                pass

            @task
            def my_task_2(self):
                pass

            host = "http://example.com"

        class MyUser2(User):
            host = "http://example.com"

        self.environment.user_classes = [MyUser, MyUser2]
        self.environment.available_user_classes = {"User1": MyUser, "User2": MyUser2}
        self.environment.available_user_tasks = {"User1": MyUser.tasks, "User2": MyUser2.tasks}

        requests.post(
            "http://127.0.0.1:%i/user" % self.web_port,
            json={"user_class_name": "User1", "host": "http://localhost", "tasks": ["my_task_2"]},
        )

        self.assertEqual(
            self.environment.available_user_classes["User1"].json(),
            {"host": "http://localhost", "tasks": ["my_task_2"], "fixed_count": 0, "weight": 1},
        )


class TestWebUIAuth(LocustTestCase):
    def setUp(self):
        super().setUp()

        parser = get_parser(default_config_files=[])
        self.environment.parsed_options = parser.parse_args(["--web-login"])

        self.web_ui = self.environment.create_web_ui("127.0.0.1", 0, web_login=True)

        self.web_ui.app.secret_key = "secret!"
        gevent.sleep(0.01)
        self.web_port = self.web_ui.server.server_port

    def tearDown(self):
        super().tearDown()
        self.web_ui.stop()
        self.runner.quit()

    def test_index_with_web_login_enabled_valid_user(self):
        class User(UserMixin):
            def __init__(self):
                self.username = "test_user"

            def get_id(self):
                return self.username

        def load_user(id):
            return User()

        self.web_ui.login_manager.request_loader(load_user)

        response = requests.get("http://127.0.0.1:%i" % self.web_port)
        d = pq(response.content.decode("utf-8"))

        self.assertNotIn("authArgs", str(d))
        self.assertIn("templateArgs", str(d))

    def test_index_with_web_login_enabled_no_user(self):
        def load_user():
            return None

        self.web_ui.login_manager.user_loader(load_user)

        response = requests.get("http://127.0.0.1:%i" % self.web_port)
        d = pq(response.content.decode("utf-8"))

        # asserts auth page is returned
        self.assertIn("authArgs", str(d))


class TestWebUIWithTLS(LocustTestCase):
    def setUp(self):
        super().setUp()
        tls_cert, tls_key = create_tls_cert("127.0.0.1")
        self.tls_cert_file = NamedTemporaryFile(delete=False)
        self.tls_key_file = NamedTemporaryFile(delete=False)
        with open(self.tls_cert_file.name, "w") as f:
            f.write(tls_cert.decode())
        with open(self.tls_key_file.name, "w") as f:
            f.write(tls_key.decode())

        parser = get_parser(default_config_files=[])
        options = parser.parse_args(
            [
                "--tls-cert",
                self.tls_cert_file.name,
                "--tls-key",
                self.tls_key_file.name,
            ]
        )
        self.runner = Runner(self.environment)
        self.stats = self.runner.stats
        self.web_ui = self.environment.create_web_ui("127.0.0.1", 0, tls_cert=options.tls_cert, tls_key=options.tls_key)
        gevent.sleep(0.01)
        self.web_port = self.web_ui.server.server_port

    def tearDown(self):
        super().tearDown()
        self.web_ui.stop()
        self.runner.quit()
        os.unlink(self.tls_cert_file.name)
        os.unlink(self.tls_key_file.name)

    def test_index_with_https(self):
        # Suppress only the single warning from urllib3 needed.
        from urllib3.exceptions import InsecureRequestWarning

        requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)
        self.assertEqual(200, requests.get("https://127.0.0.1:%i/" % self.web_port, verify=False).status_code)


class TestWebUIFullHistory(LocustTestCase, _HeaderCheckMixin):
    STATS_BASE_DIR = "csv_output"
    STATS_BASE_NAME = "web_test"
    STATS_FILENAME = f"{STATS_BASE_NAME}_stats.csv"
    STATS_HISTORY_FILENAME = f"{STATS_BASE_NAME}_stats_history.csv"
    STATS_FAILURES_FILENAME = f"{STATS_BASE_NAME}_failures.csv"

    def setUp(self):
        super().setUp()
        self.remove_files_if_exists()

        parser = get_parser(default_config_files=[])
        self.environment.parsed_options = parser.parse_args(
            ["--csv", os.path.join(self.STATS_BASE_DIR, self.STATS_BASE_NAME), "--csv-full-history"]
        )
        self.stats = self.environment.stats
        self.stats.CSV_STATS_INTERVAL_SEC = 0.02

        locust.stats.CSV_STATS_INTERVAL_SEC = 0.1
        self.stats_csv_writer = StatsCSVFileWriter(
            self.environment, stats.PERCENTILES_TO_REPORT, self.STATS_BASE_NAME, full_history=True
        )
        self.web_ui = self.environment.create_web_ui("127.0.0.1", 0, stats_csv_writer=self.stats_csv_writer)
        self.web_ui.app.view_functions["locust.request_stats"].clear_cache()
        gevent.sleep(0.01)
        self.web_port = self.web_ui.server.server_port

    def tearDown(self):
        super().tearDown()
        self.web_ui.stop()
        self.runner.quit()
        self.remove_files_if_exists()

    def remove_file_if_exists(self, filename):
        if os.path.exists(filename):
            os.remove(filename)

    def remove_files_if_exists(self):
        self.remove_file_if_exists(self.STATS_FILENAME)
        self.remove_file_if_exists(self.STATS_HISTORY_FILENAME)
        self.remove_file_if_exists(self.STATS_FAILURES_FILENAME)
        self.remove_file_if_exists(self.STATS_BASE_DIR)

    def test_request_stats_full_history_csv(self):
        self.stats.log_request("GET", "/test", 1.39764125, 2)
        self.stats.log_request("GET", "/test", 999.9764125, 1000)
        self.stats.log_request("GET", "/test2", 120, 5612)

        greenlet = gevent.spawn(self.stats_csv_writer.stats_writer)
        gevent.sleep(0.01)
        self.stats_csv_writer.stats_history_flush()
        gevent.kill(greenlet)

        response = requests.get("http://127.0.0.1:%i/stats/requests_full_history/csv" % self.web_port)
        self.assertEqual(200, response.status_code)
        self._check_csv_headers(response.headers, "requests_full_history")
        self.assertIn("Content-Length", response.headers)

        reader = csv.reader(StringIO(response.text))
        rows = [r for r in reader]

        self.assertEqual(4, len(rows))
        self.assertEqual("Timestamp", rows[0][0])
        self.assertEqual("GET", rows[1][2])
        self.assertEqual("/test", rows[1][3])
        self.assertEqual("/test2", rows[2][3])
        self.assertEqual("", rows[3][2])
        self.assertEqual("Aggregated", rows[3][3])
