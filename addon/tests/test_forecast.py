import unittest
from datetime import datetime, timedelta, timezone

from varmeopt.forecast import Forecast

ENTITY = "weather.forecast_home"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def response(*pairs, entity=ENTITY):
    """(timer frem, grader) -> et svar som weather.get_forecasts giver det."""
    return {
        entity: {
            "forecast": [
                {
                    "datetime": (NOW + timedelta(hours=h)).isoformat(),
                    "temperature": t,
                    "condition": "cloudy",
                }
                for h, t in pairs
            ]
        }
    }


class ParseTest(unittest.TestCase):
    def test_hours_become_minutes_ahead(self):
        f = Forecast.from_response(response((0, 15.0), (1, 13.0), (2, 11.0)), ENTITY, NOW)

        self.assertEqual([m for m, _ in f.points], [0.0, 60.0, 120.0])
        self.assertEqual(f.horizon_minutes, 120.0)

    def test_points_in_the_past_are_dropped(self):
        # En udsigt der begynder i gaar siger intet om i aften.
        f = Forecast.from_response(response((-5, 20.0), (1, 13.0)), ENTITY, NOW)

        self.assertEqual(len(f), 1)
        self.assertAlmostEqual(f.points[0][1], 13.0)

    def test_a_point_just_behind_us_is_kept_as_now(self):
        f = Forecast.from_response(response((-0.5, 16.0), (1, 13.0)), ENTITY, NOW)

        self.assertEqual(f.points[0][0], 0.0)

    def test_rows_without_a_temperature_are_skipped(self):
        raw = response((1, 13.0))
        raw[ENTITY]["forecast"].append({"datetime": NOW.isoformat()})

        self.assertEqual(len(Forecast.from_response(raw, ENTITY, NOW)), 1)

    def test_a_response_without_the_entity_key_still_works(self):
        # Nogle udgaver svarer uden at gentage entitets-id'et.
        raw = {"forecast": [{"datetime": NOW.isoformat(), "temperature": 12.0}]}

        self.assertEqual(len(Forecast.from_response(raw, ENTITY, NOW)), 1)

    def test_garbage_gives_an_empty_forecast(self):
        for junk in (None, "ikke en udsigt", {}, {ENTITY: {"forecast": "aeh"}}):
            f = Forecast.from_response(junk, ENTITY, NOW)
            self.assertEqual(len(f), 0)
            self.assertIsNone(f.temperature_at(60))

    def test_the_z_suffix_is_understood(self):
        raw = {ENTITY: {"forecast": [{"datetime": "2026-09-02T13:00:00Z", "temperature": 9.0}]}}

        f = Forecast.from_response(raw, ENTITY, NOW)

        self.assertEqual(f.points[0][0], 60.0)


class LookupTest(unittest.TestCase):
    def setUp(self):
        self.f = Forecast.from_response(
            response((0, 15.0), (1, 13.0), (2, 11.0), (3, 5.0)), ENTITY, NOW
        )

    def test_exact_points(self):
        self.assertAlmostEqual(self.f.temperature_at(60), 13.0)

    def test_interpolates_between_hours(self):
        self.assertAlmostEqual(self.f.temperature_at(90), 12.0)
        self.assertAlmostEqual(self.f.temperature_at(30), 14.0)

    def test_clamps_beyond_the_horizon(self):
        # At forlaenge en temperaturkurve ud i det blaa ville finde paa tal
        # som ingen har lovet os.
        self.assertAlmostEqual(self.f.temperature_at(600), 5.0)

    def test_clamps_before_the_first_point(self):
        self.assertAlmostEqual(self.f.temperature_at(-30), 15.0)


class ChainTest(unittest.TestCase):
    """Hele kaeden: udsigt -> varmekurve -> setpunkt -> COP."""

    def test_a_colder_evening_gives_a_lower_cop(self):
        from varmeopt.cop import Cell, CopTable
        from varmeopt.curve import HeatCurve, Point

        curve = HeatCurve({5: Point(44.0, 500.0), 15: Point(32.0, 500.0)})
        table = CopTable(
            {44: {5: Cell(3.9, 300.0)}, 32: {15: Cell(4.6, 300.0)}}
        )
        f = Forecast.from_response(response((0, 15.0), (6, 5.0)), ENTITY, NOW)

        def cop_at(minutes: int) -> float:
            temp = f.temperature_at(minutes)
            return table.lookup(curve.predict(temp), temp).cop

        now = cop_at(0)
        evening = cop_at(360)

        self.assertGreater(now, evening)
        self.assertAlmostEqual(now, 4.6, places=1)
        self.assertAlmostEqual(evening, 3.9, places=1)


if __name__ == "__main__":
    unittest.main()
