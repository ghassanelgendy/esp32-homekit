#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/time.h>
#include <unistd.h>
#include <AudioToolbox/AudioToolbox.h>
#include <AudioToolbox/AudioSession.h>


#define SAMPLE_RATE 16000
#define BUFFER_SIZE 800 // 50ms at 16kHz
#define NUM_BUFFERS 3

// State structure
typedef struct {
    double last_spike_time;
    double last_trigger_time;
    float ambient_average;
} ClapState;

// Helper to get time in seconds
double get_time_seconds() {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (double)tv.tv_sec + (double)tv.tv_usec / 1000000.0;
}

// AudioQueue callback
void handle_buffer(void *inUserData, AudioQueueRef inAQ, AudioQueueBufferRef inBuffer, 
                   const AudioTimeStamp *inStartTime, UInt32 inNumPackets, 
                   const AudioStreamPacketDescription *inPacketDesc) {
    
    ClapState *state = (ClapState *)inUserData;
    int16_t *samples = (int16_t *)inBuffer->mAudioData;
    UInt32 num_samples = inBuffer->mAudioDataByteSize / sizeof(int16_t);
    
    if (num_samples == 0) return;
    
    // Find peak amplitude in this buffer
    int32_t max_val = 0;
    for (UInt32 i = 0; i < num_samples; i++) {
        int32_t val = abs(samples[i]);
        if (val > max_val) {
            max_val = val;
        }
    }
    
    double now = get_time_seconds();
    
    // Update ambient average (running average of peak amplitudes)
    state->ambient_average = state->ambient_average * 0.98f + max_val * 0.02f;
    
    // Keep ambient average within a sensible range [500.0, 10000.0]
    if (state->ambient_average < 500.0f) {
        state->ambient_average = 500.0f;
    } else if (state->ambient_average > 10000.0f) {
        state->ambient_average = 10000.0f;
    }
    
    // Threshold condition:
    // 1. Peak is high absolute value (exceeds 20000 out of 32767)
    // 2. Peak is significantly higher than ambient average (at least 2.0 times louder than background noise)
    // 3. Cooldown after last trigger has expired (1.5 seconds)
    if (max_val > 20000 && max_val > (state->ambient_average * 2.0f) && (now - state->last_trigger_time) > 1.5) {
        double time_since_last_spike = now - state->last_spike_time;
        
        printf("[Clap Tweak] Spike detected! Peak: %d, Ambient: %.1f, Interval: %.3fs\n", 
               max_val, state->ambient_average, time_since_last_spike);
        
        // Double clap detection: two spikes separated by 0.12 to 0.70 seconds
        if (time_since_last_spike > 0.12 && time_since_last_spike < 0.70) {
            printf("[Clap Tweak] DOUBLE CLAP CONFIRMED! Toggling AC...\n");
            system("curl -s -X POST http://YOUR_SERVER_IP:8123/api/webhook/clap_ac_toggle &");
            state->last_trigger_time = now;
            state->last_spike_time = 0; // Reset
        } else {
            state->last_spike_time = now;
        }
    }
    
    // Re-enqueue the buffer
    AudioQueueEnqueueBuffer(inAQ, inBuffer, 0, NULL);
}

int main() {
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);
    printf("Starting Native Clap Detector on iPhone 4S...\n");
    
    // Initialize and configure AudioSession to allow mixing with background playback (AirSpeaker)
    OSStatus session_status = AudioSessionInitialize(NULL, NULL, NULL, NULL);
    if (session_status == noErr) {
        UInt32 category = kAudioSessionCategory_PlayAndRecord;
        AudioSessionSetProperty(kAudioSessionProperty_AudioCategory, sizeof(category), &category);
        
        UInt32 mixWithOthers = 1;
        AudioSessionSetProperty(kAudioSessionProperty_OverrideCategoryMixWithOthers, sizeof(mixWithOthers), &mixWithOthers);
        
        UInt32 route = kAudioSessionOverrideAudioRoute_Speaker;
        AudioSessionSetProperty(kAudioSessionProperty_OverrideAudioRoute, sizeof(route), &route);
        
        AudioSessionSetActive(true);
        printf("[Clap Tweak] AudioSession configured successfully (PlayAndRecord + MixWithOthers + SpeakerRoute).\n");
    } else {
        fprintf(stderr, "[Clap Tweak] Error initializing AudioSession: %d\n", (int)session_status);
    }

    
    ClapState state;
    state.last_spike_time = 0;
    state.last_trigger_time = 0;
    state.ambient_average = 1000.0f; // Initial estimate
    
    AudioStreamBasicDescription format;
    memset(&format, 0, sizeof(format));
    format.mSampleRate = SAMPLE_RATE;
    format.mFormatID = kAudioFormatLinearPCM;
    format.mFormatFlags = kLinearPCMFormatFlagIsSignedInteger | kLinearPCMFormatFlagIsPacked;
    format.mBytesPerPacket = 2;
    format.mFramesPerPacket = 1;
    format.mBytesPerFrame = 2;
    format.mChannelsPerFrame = 1;
    format.mBitsPerChannel = 16;
    
    AudioQueueRef queue;
    // Pass NULL for run loop to run callbacks on AudioQueue's internal dispatcher thread
    OSStatus status = AudioQueueNewInput(&format, handle_buffer, &state, NULL, NULL, 0, &queue);
    if (status != noErr) {
        fprintf(stderr, "Error creating AudioQueue input: %d\n", (int)status);
        return 1;
    }
    
    AudioQueueBufferRef buffers[NUM_BUFFERS];
    UInt32 buffer_byte_size = BUFFER_SIZE * sizeof(int16_t);
    
    for (int i = 0; i < NUM_BUFFERS; i++) {
        status = AudioQueueAllocateBuffer(queue, buffer_byte_size, &buffers[i]);
        if (status != noErr) {
            fprintf(stderr, "Error allocating buffer: %d\n", (int)status);
            return 1;
        }
        status = AudioQueueEnqueueBuffer(queue, buffers[i], 0, NULL);
        if (status != noErr) {
            fprintf(stderr, "Error enqueuing buffer: %d\n", (int)status);
            return 1;
        }
    }
    
    status = AudioQueueStart(queue, NULL);
    if (status != noErr) {
        fprintf(stderr, "Error starting AudioQueue: %d\n", (int)status);
        return 1;
    }
    
    printf("Clap detector is active. Listening for double claps...\n");
    
    // Main thread just sleeps and lets the AudioQueue thread handle callbacks
    while (1) {
        sleep(1);
    }
    
    return 0;
}
