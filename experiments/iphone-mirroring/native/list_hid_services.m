// Read-only service inventory. No callbacks, event subscriptions, or posting.
#import <Foundation/Foundation.h>
#import <dlfcn.h>
int main(void) { @autoreleasepool {
 void *h=dlopen("/System/Library/Frameworks/IOKit.framework/IOKit",RTLD_NOW);
 CFTypeRef(*create)(CFAllocatorRef)=dlsym(h,"IOHIDEventSystemClientCreate");
 CFArrayRef(*services)(CFTypeRef)=dlsym(h,"IOHIDEventSystemClientCopyServices");
 CFNumberRef(*identifier)(CFTypeRef)=dlsym(h,"IOHIDServiceClientGetRegistryID");
 CFTypeRef(*property)(CFTypeRef,CFStringRef)=dlsym(h,"IOHIDServiceClientCopyProperty");
 if(!create||!services||!identifier||!property){fprintf(stderr,"missing symbol\n");return 2;}
 CFTypeRef c=create(kCFAllocatorDefault);
 if(!c){fprintf(stderr,"client unavailable\n");return 3;}
 CFArrayRef a=services(c);
 NSMutableArray *out=[NSMutableArray array];
 for(CFIndex i=0;a && i<CFArrayGetCount(a);i++) {
  CFTypeRef s=CFArrayGetValueAtIndex(a,i); uint64_t sid=0; CFNumberRef idValue=identifier(s); if(idValue)CFNumberGetValue(idValue,kCFNumberSInt64Type,&sid);
  CFTypeRef p=property(s,CFSTR("Product"));
  [out addObject:@{@"serviceID":@(sid),@"serviceIDHex":[NSString stringWithFormat:@"0x%llx",(unsigned long long)sid],@"product": p ? (__bridge id)p : @""}];
  if(p)CFRelease(p);
 }
 NSData *json=[NSJSONSerialization dataWithJSONObject:out options:NSJSONWritingPrettyPrinted error:NULL];
 fwrite(json.bytes,1,json.length,stdout);fputc('\n',stdout);
 if(a)CFRelease(a);CFRelease(c);return 0;
} }
