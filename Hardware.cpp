#include <Arduino.h>
#include <Wire.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <DFRobot_PH.h>
#include <EEPROM.h>
#include <math.h>
#include <string.h>

// --- SENSOR PIN DEFINITIONS ---
#define ONE_WIRE_BUS 10
#define Turbidity_SENSOR_PIN 6
#define PH_SENSOR_PIN 9

#define SYSTEM_VOLTAGE 3300.0f
#define ADC_RESOLUTION 4095.0f

// 1 = Serial Monitor debugging.
// 0 = Raspberry Pi binary communication.
// Set to 0 before communicating with the Pi.
#define DEBUG_PRINT 1

#define BUFFER_SIZE 32

// --- MANUAL PH CONTROL ---
// false = pH disabled: display and transmit NAN.
// true  = read pH from the module.
//
// Change this setting and upload again when needed.
// This is NOT automatic probe-disconnection detection.
const bool PH_ENABLED = true;

// --- SENSOR VARIABLES ---
float voltage = NAN;
float phValue = NAN;

float Celsius = NAN;
float Fahrenheit = NAN;

float turbidityVoltage = 0;
int turbidityRaw = 0;

// Indicates library initialization, NOT probe connection.
bool phReady = false;

// --- SENSOR OBJECTS ---
DFRobot_PH ph;
OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature sensors(&oneWire);

// --- USB BUFFER ---
uint8_t sendbuf[BUFFER_SIZE] = {0};

// --- SENSOR TIMER ---
unsigned long lastSensorTime = 0;
const unsigned long SENSOR_INTERVAL = 2000;

// --- DATA PACKET: EXACTLY 32 BYTES ---
typedef struct {
    float temperature;       // Bytes 0–3: Celsius
    float ph;                // Bytes 4–7
    float turbidity_raw;     // Bytes 8–11
    float turbidity_volt;    // Bytes 12–15
    uint32_t timestamp;      // Bytes 16–19
    uint8_t checksum;        // Byte 20
    uint8_t reserved[11];    // Bytes 21–31
} __attribute__((packed)) SensorData;

static_assert(
    sizeof(SensorData) == BUFFER_SIZE,
    "SensorData must be exactly 32 bytes"
);

SensorData sensorData = {};

// --- TEMPERATURE SENSOR ---
void readTemperatureSensor() {
    // Retry detection if no sensor was previously found.
    if (sensors.getDeviceCount() == 0) {
        sensors.begin();
    }

    sensors.requestTemperatures();

    float temperatureC = sensors.getTempCByIndex(0);

    // Check that the reading is valid.
    if (temperatureC == DEVICE_DISCONNECTED_C ||
        !isfinite(temperatureC)) {

        Celsius = NAN;
        Fahrenheit = NAN;

#if DEBUG_PRINT
        Serial.print("Temp: sensor error | ");
#endif

        return;
    }

    Celsius = temperatureC;
    Fahrenheit = sensors.toFahrenheit(Celsius);

#if DEBUG_PRINT
    Serial.print("Temp: ");
    Serial.print(Celsius, 2);
    Serial.print(" °C / ");
    Serial.print(Fahrenheit, 2);
    Serial.print(" °F | ");
#endif
}

// --- PH SENSOR ---
void readPHSensor() {
    // Clear the previous values on every read.
    voltage = NAN;
    phValue = NAN;

    // Manual disable: do not read the pH module.
    if (!PH_ENABLED) {
#if DEBUG_PRINT
        Serial.print("pH: NaN (disabled) | ");
#endif
        return;
    }

    if (!phReady) {
#if DEBUG_PRINT
        Serial.print("pH: NaN (library/EEPROM unavailable) | ");
#endif
        return;
    }

    const int sampleCount = 16;
    uint32_t totalMillivolts = 0;

    for (int i = 0; i < sampleCount; i++) {
        totalMillivolts += analogReadMilliVolts(PH_SENSOR_PIN);
    }

    voltage = (float)totalMillivolts / sampleCount;

    // Retains your original library assumption:
    // the DFRobot_PH readPH() implementation being used
    // does not use its temperature argument.
    //
    // A different implementation that uses temperature
    // must be reviewed before passing NAN here.
    float measuredPH = ph.readPH(voltage, NAN);

    if (isfinite(measuredPH)) {
        phValue = measuredPH;
    }

#if DEBUG_PRINT
    Serial.print("pH: ");

    if (isfinite(phValue)) {
        Serial.print(phValue, 2);

        if (phValue < 0.0f || phValue > 14.0f) {
            Serial.print(" (out of range)");
        }
    } else {
        Serial.print("NaN (invalid reading)");
    }

    Serial.print(" | pH input: ");
    Serial.print(voltage, 1);
    Serial.print(" mV | ");
#endif

    // Uses existing/default library reference values.
    // No Serial calibration handler is called.
}

// --- TURBIDITY SENSOR ---
void readTurbiditySensor() {
    turbidityRaw = analogRead(Turbidity_SENSOR_PIN);

    // Preserve the original approximate conversion.
    turbidityVoltage =
        turbidityRaw / ADC_RESOLUTION *
        (SYSTEM_VOLTAGE / 1000.0f);

#if DEBUG_PRINT
    Serial.print("Turbidity: ");
    Serial.print(turbidityRaw);
    Serial.print(" (");
    Serial.print(turbidityVoltage, 2);
    Serial.println(" V)");
#endif
}

// --- READ ALL SENSORS ---
void readAllSensors() {
#if DEBUG_PRINT
    Serial.println("\n--- Reading Sensors ---");
#endif

    readTemperatureSensor();
    readPHSensor();
    readTurbiditySensor();
}

// --- CHECKSUM ---
uint8_t calculateChecksum(const uint8_t* data, int length) {
    uint8_t sum = 0;

    for (int i = 0; i < length; i++) {
        sum += data[i];
    }

    return sum;
}

// --- PREPARE PACKET ---
void prepareSensorDataPacket() {
    memset(&sensorData, 0, sizeof(sensorData));

    sensorData.temperature = Celsius;

    // Contains NAN when pH is disabled or unavailable.
    sensorData.ph = phValue;

    sensorData.turbidity_raw = (float)turbidityRaw;
    sensorData.turbidity_volt = turbidityVoltage;
    sensorData.timestamp = millis();

    // Sum bytes 0–19, excluding checksum and reserved bytes.
    sensorData.checksum = calculateChecksum(
        reinterpret_cast<const uint8_t*>(&sensorData),
        20
    );

    memcpy(sendbuf, &sensorData, sizeof(sensorData));

#if DEBUG_PRINT
    Serial.println("Sensor data packet prepared for transmission");
#endif
}

// --- SETUP ---
void setup() {
    Serial.begin(115200);
    delay(2000);

#if DEBUG_PRINT
    Serial.println("\n=== ESP32-S3 Sensor + USB Serial System ===");
#endif

    // Initialize the pH library only when pH is enabled.
    if (PH_ENABLED) {
        if (EEPROM.begin(512)) {
            ph.begin();
            phReady = true;
        } else {
#if DEBUG_PRINT
            Serial.println("EEPROM initialization failed.");
#endif
        }
    } else {
#if DEBUG_PRINT
        Serial.println("pH measurement manually disabled.");
#endif
    }

    sensors.begin();
    sensors.setWaitForConversion(true);

    analogReadResolution(12);
    analogSetAttenuation(ADC_11db);

#if DEBUG_PRINT
    Serial.print("Temperature sensors detected: ");
    Serial.println(sensors.getDeviceCount());

    Serial.println("System ready.\n");
#endif

    readAllSensors();
    prepareSensorDataPacket();

    lastSensorTime = millis();
}

// --- MAIN LOOP ---
void loop() {
    // 1. Refresh sensor readings approximately every 2 seconds.
    unsigned long currentTime = millis();

    if (currentTime - lastSensorTime >= SENSOR_INTERVAL) {
        lastSensorTime = currentTime;

        readAllSensors();
        prepareSensorDataPacket();
    }

    // 2. Respond to a Raspberry Pi request.
    if (Serial.available() > 0) {
        uint8_t incomingByte = Serial.read();

        if (incomingByte == 0xAA) {
            delay(2);

            // Preserve the original request-buffer handling.
            while (Serial.available() > 0) {
                Serial.read();
            }

            // Two-byte synchronization header.
            Serial.write((uint8_t)0xFF);
            Serial.write((uint8_t)0xAA);

            // 32-byte sensor packet.
            Serial.write(sendbuf, BUFFER_SIZE);
        }
    }
}
