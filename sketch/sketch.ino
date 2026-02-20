/**
 * Air Quality Monitor — MCU sketch for Arduino UNO Q
 *
 * Reads DPS310 (pressure + temperature) and SCD-30 (CO₂ + temperature +
 * humidity) every 2 seconds, then sends the readings to the Linux MPU over
 * the Bridge so the Flask web-app can display and upload them.
 *
 * Wire both sensors to the Qwiic / I²C bus.
 *
 * Payload format sent via Bridge.notify("sensor_update", …):
 *   "co2,tempSCD,humidity,pressure,tempDPS"
 *   All values are floating-point ASCII, comma-separated.
 *
 * Optional sensors (VOC / particulates):
 *   Uncomment the relevant sections below and add the corresponding
 *   libraries to sketch.yaml.
 */

#include <Arduino_RouterBridge.h>
#include <ArduinoGraphics.h>
#include <Arduino_LED_Matrix.h>
#include <Wire.h>
#include <Adafruit_DPS310.h>
#include <SparkFun_SCD30_Arduino_Library.h>

// ── Optional: SGP40 VOC sensor ────────────────────────────────────────────
// #include <Adafruit_SGP40.h>
// Adafruit_SGP40 sgp40;
// bool hasVOC = false;

// ── Optional: SPS30 particulate sensor ───────────────────────────────────
// #include <SensirionI2cSps30.h>
// SensirionI2cSps30 sps30;
// bool hasPM = false;

ArduinoLEDMatrix matrix;
Adafruit_DPS310   dps310;
SCD30             scd30;

bool hasDPS310 = false;
bool hasSCD30  = false;

// Latest readings (updated in readSensors())
float co2      = 0.0f;
float tempSCD  = 0.0f;
float humidity = 0.0f;
float pressure = 0.0f;
float tempDPS  = 0.0f;
// float vocIndex = 0.0f;  // uncomment for SGP40
// float pm25     = 0.0f;  // uncomment for SPS30
// float pm10     = 0.0f;

unsigned long lastSend = 0;
const unsigned long SEND_INTERVAL = 2000UL; // ms

// ── LED Matrix helper ──────────────────────────────────────────────────────
void ledScroll(const char* text) {
    matrix.beginDraw();
    matrix.stroke(0xFFFFFFFF);
    matrix.textScrollSpeed(80);
    matrix.textFont(Font_4x6);
    matrix.beginText(0, 1, 0xFFFFFF);
    matrix.println(text);
    matrix.endText(SCROLL_LEFT);
    matrix.endDraw();
}

// ── Bridge callback: Python requests an immediate data push ───────────────
void onPing(float /*dummy*/) {
    pushSensorData();
}

// ── Sensor reading ─────────────────────────────────────────────────────────
void readSensors() {
    if (hasDPS310) {
        sensors_event_t tEv, pEv;
        if (dps310.getEvents(&tEv, &pEv)) {
            pressure = pEv.pressure;   // hPa
            tempDPS  = tEv.temperature; // °C
        }
    }

    if (hasSCD30 && scd30.dataAvailable()) {
        co2      = scd30.getCO2();         // ppm
        tempSCD  = scd30.getTemperature(); // °C
        humidity = scd30.getHumidity();    // %RH
    }

    // ── Optional SGP40 ──────────────────────────────────────────────────
    // if (hasVOC) {
    //     uint32_t voc_raw = sgp40.measureVoc();
    //     vocIndex = (float)voc_raw; // raw ticks; convert with SensirionVOCAlgorithm if needed
    // }

    // ── Optional SPS30 ──────────────────────────────────────────────────
    // if (hasPM) {
    //     struct sps30_measurement m;
    //     if (sps30.readMeasurement(&m) == 0) {
    //         pm25 = m.mc_2p5;
    //         pm10 = m.mc_10p0;
    //     }
    // }
}

// ── Build and send sensor payload ─────────────────────────────────────────
void pushSensorData() {
    // Format: co2,tempSCD,humidity,pressure,tempDPS
    // Extend with ",vocIndex,pm25,pm10" if optional sensors are enabled.
    String payload =
        String(co2,      1) + "," +
        String(tempSCD,  2) + "," +
        String(humidity, 2) + "," +
        String(pressure, 2) + "," +
        String(tempDPS,  2);

    Bridge.notify("sensor_update", payload);
}

// ── Setup ──────────────────────────────────────────────────────────────────
void setup() {
    Serial.begin(115200);
    Wire.begin();
    matrix.begin();
    ledScroll("INIT");

    Bridge.begin();
    Bridge.provide_safe("ping", onPing);

    // DPS310
    if (dps310.begin_I2C()) {
        dps310.configurePressure(DPS310_64HZ, DPS310_64SAMPLES);
        dps310.configureTemperature(DPS310_64HZ, DPS310_64SAMPLES);
        hasDPS310 = true;
        Serial.println("[DPS310] OK");
    } else {
        Serial.println("[DPS310] not found");
    }

    // SCD-30
    if (scd30.begin()) {
        scd30.setMeasurementInterval(2); // minimum 2 s
        hasSCD30 = true;
        Serial.println("[SCD30] OK");
    } else {
        Serial.println("[SCD30] not found");
    }

    // ── Optional SGP40 ──────────────────────────────────────────────────
    // if (sgp40.begin()) { hasVOC = true; Serial.println("[SGP40] OK"); }

    // ── Optional SPS30 ──────────────────────────────────────────────────
    // sps30.begin(Wire);
    // if (sps30.startMeasurement() == 0) { hasPM = true; Serial.println("[SPS30] OK"); }

    // Show sensor status on LED matrix: D=DPS310 C=SCD30
    String st = "S:";
    st += hasDPS310 ? "D" : "-";
    st += hasSCD30  ? "C" : "-";
    ledScroll(st.c_str());
}

// ── Loop ───────────────────────────────────────────────────────────────────
void loop() {
    unsigned long now = millis();
    if (now - lastSend >= SEND_INTERVAL) {
        lastSend = now;
        readSensors();
        pushSensorData();
    }
    delay(50);
}
