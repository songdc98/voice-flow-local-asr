#import <Cocoa/Cocoa.h>
#import <Carbon/Carbon.h>
#import <AVFoundation/AVFoundation.h>
#import <signal.h>

@interface VoiceFlowLauncherDelegate : NSObject <NSApplicationDelegate, NSSpeechRecognizerDelegate>
@property(nonatomic, strong) NSTask *task;
@property(nonatomic, strong) NSSpeechRecognizer *speechRecognizer;
@property(nonatomic, assign) EventHotKeyRef recordHotKey;
@property(nonatomic, assign) EventHotKeyRef copyHotKey;
@property(nonatomic, assign) NSTimeInterval lastPasteRequestTime;
@property(nonatomic, copy) NSString *projectDir;
@property(nonatomic, copy) NSArray<NSString *> *wakePhrases;
@property(nonatomic, copy) NSString *stopPhrase;
- (void)sendVoiceSignal:(int)signalNumber;
@end

static OSStatus VoiceFlowHotKeyHandler(
    EventHandlerCallRef nextHandler,
    EventRef event,
    void *userData
) {
    (void)nextHandler;
    EventHotKeyID hotKeyID;
    OSStatus status = GetEventParameter(
        event,
        kEventParamDirectObject,
        typeEventHotKeyID,
        NULL,
        sizeof(hotKeyID),
        NULL,
        &hotKeyID
    );
    if (status != noErr) {
        return status;
    }

    VoiceFlowLauncherDelegate *delegate = (__bridge VoiceFlowLauncherDelegate *)userData;
    if (hotKeyID.id == 1) {
        [delegate sendVoiceSignal:SIGUSR1];
    } else if (hotKeyID.id == 2) {
        [delegate sendVoiceSignal:SIGUSR2];
    }
    return noErr;
}

@implementation VoiceFlowLauncherDelegate

- (NSString *)resolvedProjectDir {
    NSString *configured = [[NSBundle mainBundle] objectForInfoDictionaryKey:@"VoiceFlowProjectPath"];
    if (configured.length > 0) {
        return [configured stringByStandardizingPath];
    }
    return @"/Users/song/Research/local_asr_qwen3";
}

- (NSString *)pathInProject:(NSString *)relativePath {
    return [self.projectDir stringByAppendingPathComponent:relativePath];
}

- (NSDictionary *)configDictionary {
    NSData *data = [NSData dataWithContentsOfFile:[self pathInProject:@"config.json"]];
    if (data == nil) {
        return @{};
    }
    NSDictionary *payload = [NSJSONSerialization JSONObjectWithData:data
                                                            options:0
                                                              error:nil];
    if (![payload isKindOfClass:[NSDictionary class]]) {
        return @{};
    }
    return payload;
}

- (NSDictionary *)voiceTriggerConfig {
    NSDictionary *voiceTrigger = [self configDictionary][@"voice_trigger"];
    if (![voiceTrigger isKindOfClass:[NSDictionary class]]) {
        return @{};
    }
    return voiceTrigger;
}

- (BOOL)voiceTriggerEnabled {
    id enabled = [self voiceTriggerConfig][@"enabled"];
    if (enabled == nil) {
        return YES;
    }
    return [enabled boolValue];
}

- (void)loadVoiceTriggerPhrases {
    NSDictionary *voiceTrigger = [self voiceTriggerConfig];
    NSMutableArray<NSString *> *phrases = [NSMutableArray array];
    id configuredPhrases = voiceTrigger[@"wake_phrases"];
    if ([configuredPhrases isKindOfClass:[NSArray class]]) {
        for (id item in configuredPhrases) {
            if ([item isKindOfClass:[NSString class]] && [item length] > 0 && ![phrases containsObject:item]) {
                [phrases addObject:item];
            }
        }
    }
    NSString *legacyWake = voiceTrigger[@"wake_phrase"];
    if (legacyWake.length > 0 && ![phrases containsObject:legacyWake]) {
        [phrases addObject:legacyWake];
    }
    if (phrases.count == 0) {
        [phrases addObjectsFromArray:@[@"hey siri", @"siri"]];
    }
    NSString *stop = voiceTrigger[@"stop_phrase"];
    self.wakePhrases = phrases;
    self.stopPhrase = stop.length > 0 ? stop : @"结束";
}

- (NSString *)currentVoiceFlowState {
    NSData *data = [NSData dataWithContentsOfFile:[self pathInProject:@"voice_flow_status.json"]];
    if (data == nil) {
        return @"idle";
    }
    NSDictionary *payload = [NSJSONSerialization JSONObjectWithData:data
                                                            options:0
                                                              error:nil];
    if (![payload isKindOfClass:[NSDictionary class]]) {
        return @"idle";
    }
    NSString *state = payload[@"state"];
    if (![state isKindOfClass:[NSString class]] || state.length == 0) {
        return @"idle";
    }
    return state;
}

- (pid_t)voiceFlowPID {
    NSString *pidPath = [self pathInProject:@"voice_flow.pid"];
    NSString *pidText = [NSString stringWithContentsOfFile:pidPath
                                                  encoding:NSUTF8StringEncoding
                                                     error:nil];
    return (pid_t)[pidText intValue];
}

- (void)sendVoiceSignal:(int)signalNumber {
    pid_t pid = [self voiceFlowPID];
    if (pid > 1) {
        kill(pid, signalNumber);
    }
}

- (void)startSpeechRecognizer {
    if (![self voiceTriggerEnabled]) {
        return;
    }

    [self loadVoiceTriggerPhrases];
    self.speechRecognizer = [[NSSpeechRecognizer alloc] init];
    if (self.speechRecognizer == nil) {
        return;
    }

    self.speechRecognizer.commands = [self.wakePhrases arrayByAddingObject:self.stopPhrase];
    self.speechRecognizer.delegate = self;
    self.speechRecognizer.listensInForegroundOnly = NO;
    [self.speechRecognizer startListening];
}

- (void)stopSpeechRecognizer {
    if (self.speechRecognizer == nil) {
        return;
    }

    [self.speechRecognizer stopListening];
    self.speechRecognizer.delegate = nil;
    self.speechRecognizer = nil;
}

- (void)speechRecognizer:(NSSpeechRecognizer *)sender didRecognizeCommand:(NSString *)command {
    (void)sender;
    NSString *state = [self currentVoiceFlowState];
    if ([self.wakePhrases containsObject:command]) {
        if (![state isEqualToString:@"recording"] && ![state isEqualToString:@"processing"]) {
            [self sendVoiceSignal:SIGUSR1];
        }
        return;
    }

    if ([command isEqualToString:self.stopPhrase]) {
        if ([state isEqualToString:@"recording"]) {
            [self sendVoiceSignal:SIGUSR1];
        }
    }
}

- (void)installHotKeys {
    EventTypeSpec eventType;
    eventType.eventClass = kEventClassKeyboard;
    eventType.eventKind = kEventHotKeyPressed;
    InstallApplicationEventHandler(
        VoiceFlowHotKeyHandler,
        1,
        &eventType,
        (__bridge void *)self,
        NULL
    );

    EventHotKeyID recordID;
    recordID.signature = 0x56464c57; // VFLW
    recordID.id = 1;
    OSStatus recordStatus = RegisterEventHotKey(
        121, // Page Down
        0,
        recordID,
        GetApplicationEventTarget(),
        0,
        &_recordHotKey
    );

    EventHotKeyID copyID;
    copyID.signature = 0x56464c57; // VFLW
    copyID.id = 2;
    OSStatus copyStatus = RegisterEventHotKey(
        116, // Page Up
        0,
        copyID,
        GetApplicationEventTarget(),
        0,
        &_copyHotKey
    );

    NSString *statusText = [NSString stringWithFormat:
        @"{\"page_down\": %d, \"page_up\": %d}\n",
        (int)recordStatus,
        (int)copyStatus
    ];
    [statusText writeToFile:[self pathInProject:@"native_hotkeys.json"]
                 atomically:YES
                   encoding:NSUTF8StringEncoding
                      error:nil];
}

- (void)requestAccessibilityPrompt {
    NSDictionary *options = @{(__bridge id)kAXTrustedCheckOptionPrompt: @YES};
    AXIsProcessTrustedWithOptions((__bridge CFDictionaryRef)options);
}

- (void)writePasteStatusWithMethod:(NSString *)method
                            axCode:(AXError)axCode
                           trusted:(BOOL)trusted {
    NSDictionary *payload = @{
        @"updated_at": @([[NSDate date] timeIntervalSince1970]),
        @"method": method,
        @"ax_code": @((int)axCode),
        @"accessibility_trusted": @(trusted)
    };
    NSData *data = [NSJSONSerialization dataWithJSONObject:payload
                                                   options:0
                                                     error:nil];
    [data writeToFile:[self pathInProject:@"paste_status.json"]
           atomically:YES];
}

- (BOOL)pasteViaAccessibility {
    NSPasteboard *pasteboard = [NSPasteboard generalPasteboard];
    NSString *text = [pasteboard stringForType:NSPasteboardTypeString];
    if (text.length == 0) {
        [self writePasteStatusWithMethod:@"empty_clipboard"
                                  axCode:kAXErrorFailure
                                 trusted:AXIsProcessTrusted()];
        return NO;
    }

    AXUIElementRef systemWide = AXUIElementCreateSystemWide();
    if (systemWide == NULL) {
        [self writePasteStatusWithMethod:@"no_system_ax"
                                  axCode:kAXErrorFailure
                                 trusted:AXIsProcessTrusted()];
        return NO;
    }

    CFTypeRef focused = NULL;
    AXError copyError = AXUIElementCopyAttributeValue(
        systemWide,
        kAXFocusedUIElementAttribute,
        &focused
    );
    CFRelease(systemWide);
    if (copyError != kAXErrorSuccess || focused == NULL) {
        [self writePasteStatusWithMethod:@"no_focused_element"
                                  axCode:copyError
                                 trusted:AXIsProcessTrusted()];
        return NO;
    }

    AXError selectedTextError = AXUIElementSetAttributeValue(
        (AXUIElementRef)focused,
        kAXSelectedTextAttribute,
        (__bridge CFTypeRef)text
    );
    CFRelease(focused);
    if (selectedTextError == kAXErrorSuccess) {
        [self writePasteStatusWithMethod:@"ax_selected_text"
                                  axCode:selectedTextError
                                 trusted:AXIsProcessTrusted()];
        return YES;
    }

    [self writePasteStatusWithMethod:@"ax_selected_text_failed"
                              axCode:selectedTextError
                             trusted:AXIsProcessTrusted()];
    return NO;
}

- (void)performPaste {
    if ([self pasteViaAccessibility]) {
        return;
    }

    CGEventRef keyDown = CGEventCreateKeyboardEvent(NULL, (CGKeyCode)9, true);
    CGEventRef keyUp = CGEventCreateKeyboardEvent(NULL, (CGKeyCode)9, false);
    if (keyDown == NULL || keyUp == NULL) {
        if (keyDown != NULL) CFRelease(keyDown);
        if (keyUp != NULL) CFRelease(keyUp);
        return;
    }

    CGEventSetFlags(keyDown, kCGEventFlagMaskCommand);
    CGEventSetFlags(keyUp, kCGEventFlagMaskCommand);
    CGEventPost(kCGHIDEventTap, keyDown);
    CGEventPost(kCGHIDEventTap, keyUp);
    CFRelease(keyDown);
    CFRelease(keyUp);
    [self writePasteStatusWithMethod:@"cmd_v_event"
                              axCode:kAXErrorFailure
                             trusted:AXIsProcessTrusted()];
}

- (void)checkPasteRequest:(NSTimer *)timer {
    (void)timer;
    NSString *path = [self pathInProject:@"paste_request.json"];
    NSData *data = [NSData dataWithContentsOfFile:path];
    if (data == nil) {
        return;
    }

    NSDictionary *payload = [NSJSONSerialization JSONObjectWithData:data
                                                            options:0
                                                              error:nil];
    if (![payload isKindOfClass:[NSDictionary class]]) {
        return;
    }

    NSTimeInterval updatedAt = [payload[@"updated_at"] doubleValue];
    if (updatedAt <= self.lastPasteRequestTime) {
        return;
    }

    self.lastPasteRequestTime = updatedAt;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.28 * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{
        [self performPaste];
    });
}

- (void)ignoreExistingPasteRequest {
    NSString *path = [self pathInProject:@"paste_request.json"];
    NSData *data = [NSData dataWithContentsOfFile:path];
    if (data == nil) {
        return;
    }

    NSDictionary *payload = [NSJSONSerialization JSONObjectWithData:data
                                                            options:0
                                                              error:nil];
    if (![payload isKindOfClass:[NSDictionary class]]) {
        return;
    }

    self.lastPasteRequestTime = [payload[@"updated_at"] doubleValue];
}

- (void)uninstallHotKeys {
    if (self.recordHotKey != NULL) {
        UnregisterEventHotKey(self.recordHotKey);
        self.recordHotKey = NULL;
    }
    if (self.copyHotKey != NULL) {
        UnregisterEventHotKey(self.copyHotKey);
        self.copyHotKey = NULL;
    }
}

- (void)killVoiceFlowWorkers {
    NSTask *cleanup = [[NSTask alloc] init];
    cleanup.executableURL = [NSURL fileURLWithPath:@"/usr/bin/pkill"];
    cleanup.arguments = @[@"-f", [self pathInProject:@"voice_flow.py"]];
    [cleanup launchAndReturnError:nil];
    [cleanup waitUntilExit];
}

- (void)launchWorker {
    self.task = [[NSTask alloc] init];
    self.task.executableURL = [NSURL fileURLWithPath:[self pathInProject:@".venv/bin/python"]];
    self.task.arguments = @[[self pathInProject:@"voice_flow_menu_app.py"]];
    self.task.currentDirectoryURL = [NSURL fileURLWithPath:self.projectDir];

    __weak VoiceFlowLauncherDelegate *weakSelf = self;
    self.task.terminationHandler = ^(NSTask *task) {
        (void)task;
        dispatch_async(dispatch_get_main_queue(), ^{
            VoiceFlowLauncherDelegate *strongSelf = weakSelf;
            strongSelf.task = nil;
            [NSApp terminate:nil];
        });
    };

    NSError *error = nil;
    if (![self.task launchAndReturnError:&error]) {
        NSLog(@"Failed to launch Voice Flow: %@", error);
        [NSApp terminate:nil];
        return;
    }
    [self ignoreExistingPasteRequest];
    [self requestAccessibilityPrompt];
    [self installHotKeys];
    [self startSpeechRecognizer];
    [NSTimer scheduledTimerWithTimeInterval:0.12
                                     target:self
                                   selector:@selector(checkPasteRequest:)
                                   userInfo:nil
                                    repeats:YES];
}

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    (void)notification;
    self.projectDir = [self resolvedProjectDir];

    AVAuthorizationStatus status = [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio];
    if (status == AVAuthorizationStatusAuthorized) {
        [self launchWorker];
        return;
    }

    if (status == AVAuthorizationStatusNotDetermined) {
        [AVCaptureDevice requestAccessForMediaType:AVMediaTypeAudio
                                 completionHandler:^(BOOL granted) {
            dispatch_async(dispatch_get_main_queue(), ^{
                if (granted) {
                    [self launchWorker];
                } else {
                    NSLog(@"Voice Flow microphone permission denied.");
                    [NSApp terminate:nil];
                }
            });
        }];
        return;
    }

    NSLog(@"Voice Flow microphone permission is not authorized.");
    [NSApp terminate:nil];
}

- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)sender {
    (void)sender;
    [self stopSpeechRecognizer];
    [self uninstallHotKeys];
    if (self.task != nil && self.task.isRunning) {
        [self.task terminate];
    }
    [self killVoiceFlowWorkers];
    return NSTerminateNow;
}

- (void)applicationWillTerminate:(NSNotification *)notification {
    (void)notification;
    [self stopSpeechRecognizer];
    [self uninstallHotKeys];
    if (self.task != nil && self.task.isRunning) {
        [self.task terminate];
    }
    [self killVoiceFlowWorkers];
}

@end

int main(int argc, const char *argv[]) {
    (void)argc;
    (void)argv;
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        VoiceFlowLauncherDelegate *delegate = [[VoiceFlowLauncherDelegate alloc] init];
        app.delegate = delegate;
        [app run];
    }
    return 0;
}
